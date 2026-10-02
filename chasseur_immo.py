#!/usr/bin/env python3
"""
Veille immobilière pour marchand de biens (secteur Millau).
 
Pipeline :
  1. Collecte des liens de fiches sur les catalogues des agences (avec pagination)
  2. Extraction des infos de chaque fiche (prix, surface, titre, description)
  3. Scoring "marchand de biens" (mots-clés travaux / division / succession, prix au m²)
  4. Détection des NOUVELLES annonces depuis le dernier passage
  5. Export CSV + JSON d'état + rapport HTML (docs/index.html, publié via GitHub Pages)
"""
import csv
import json
import logging
import random
import re
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urlparse
 
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
 
# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #
DATA_DIR = Path("data")
FICHIER_ETAT = DATA_DIR / "annonces_vues.json"
FICHIER_CSV = DATA_DIR / "annonces.csv"
DOSSIER_DOCS = Path("docs")                 # servi par GitHub Pages
FICHIER_HTML = DOSSIER_DOCS / "index.html"
DEBUG_DIR = Path("debug")                    # captures si 0 annonce trouvée
 
# Zone géographique : uniquement Millau (Aveyron). Pour élargir, ajoutez des communes (en minuscules).
# NB : le code postal 12100 est aussi celui de Creissels et Saint-Georges-de-Luzençon ;
# le filtre se base donc sur le NOM de la commune, pas sur le code postal seul.
VILLES_AUTORISEES = ("millau",)
 
PRIX_MAX = 250_000          # budget maximum (None pour désactiver)
SCORE_MINIMUM = 2           # seuil pour être jugée "intéressante"
MAX_PAGES_PAR_AGENCE = 5    # pagination
MAX_FICHES_PAR_RUN = 200    # garde-fou
VERSION_PARSEUR = 6         # incrémenter quand l'extraction change : les fiches déjà vues sont ré-analysées
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
 
# Motif générique d'URL de fiche. Surchargeable par agence via "pattern_fiche".
# À AFFINER après un premier run en regardant les URLs réelles de chaque site.
PATTERN_FICHE_DEFAUT = (
    r"(/annonce|/bien/|/vente/.+|/detail|/property/|/fiche|/produit|ref-|/\d{4,}|-r\d+)"
)
 
AGENCES = [
    {
        # Roques propose une page par commune : « Voir tous les biens à Millau » (435 = identifiant de Millau).
        # On ne collecte que les fiches de la forme /vente/435-millau/<type>/<id>-<titre> :
        # les communes voisines (Montjaux, Le Caylar, Lanuéjols…) du « secteur Millau » sont exclues d'emblée.
        "nom": "Roques",
        "url": "https://www.roques-immobilier.com/vente/435-millau/1",
        "pattern_fiche": r"/vente/435-millau/[^/]+/\d+-",
        "mode": "pattern",
    },
    {"nom": "SGA", "url": "https://www.sga-immobilier.com/immobilier/immobilier-vente-millau.htm"},
    # /a-vendre renvoyait une 404 : la vraie page de vente est /resultats?transac=vente
    {"nom": "JMB", "url": "https://www.jmb-immobilier.com/resultats?transac=vente", "mode": "prix"},
    {
        # Le site est une appli JavaScript : l'ancienne URL /vente/maison/millau-12 affichait toute la France.
        # Le script saisit « Millau » dans le champ de localisation, choisit la suggestion puis lance la
        # recherche. L'URL filtrée obtenue est écrite dans le log (« URL filtrée ») : vous pouvez la coller
        # ici à la place de "url" et supprimer "preparation" pour figer le filtre.
        "nom": "Notaires",
        "url": "https://www.immobilier.notaires.fr/fr/annonces-immobilieres-liste?typeTransaction=VENTE,VNI,VAE",
        "mode": "prix",
        "preparation": "filtre_ville",
        "ville": "Millau",
    },
    {"nom": "Mesnard maisons", "url": "https://mesnard-immobilier.com/property-type/maison/",
     "pattern_fiche": r"/property/"},
    {"nom": "Mesnard apparts", "url": "https://mesnard-immobilier.com/property-type/appartement/",
     "pattern_fiche": r"/property/"},
    {"nom": "AP Immobilier", "url": "https://www.apimmobilier.fr/recherche/"},
]
 
# Pages annexes à ignorer (testées sur le chemin, pas sur tout le lien)
EXCLUS = re.compile(
    r"(contact|mentions|legal|cgu|cgv|confidential|cookies|estimation|connexion|login|"
    r"inscri|actualit|blog|recrut|honoraires|facebook|instagram|linkedin|twitter|"
    r"youtube|mailto:|tel:|javascript:|\.pdf$|\.jpg$|\.png$)",
    re.I,
)
 
# Mots-clés → points. Adapter à votre stratégie.
MOTS_CLES = {
    r"\b(à|a) r[ée]nover\b|\btravaux\b|\bà rafra[iî]chir\b|\bà restaurer\b": 3,
    r"\bimmeuble( de rapport)?\b|\bmono-?propri[ée]t[ée]\b": 3,
    r"\bdivisible\b|\bdivision\b|\bpotentiel\b|\bd[ée]tachable\b": 3,
    r"\bsuccession\b|\bvente rapide\b|\burgent\b|\bopportunit[ée]\b": 2,
    r"\bgrange\b|\bgrenier\b|\bam[ée]nageable\b|\bcombles\b|\bremise\b|\bgarage\b": 2,
    r"\binvestisseur\b|\brendement\b|\blou[ée]\b|\blocataire\b": 1,
    r"\bnégociable\b|\bbaisse de prix\b|\bprix revu\b": 2,
}
# Motifs d'exclusion. « loué » n'en fait PAS partie : un immeuble loué est au contraire intéressant
# pour un marchand de biens (il rapporte un point dans le scoring). Testés sur le titre + le début du
# contenu de la fiche SANS menu/pied de page (le menu d'une agence contient « Viager » partout).
MOTS_REDHIBITOIRES = re.compile(
    r"\b(viager|vendu|vendue|sous compromis|compromis sign[ée]|sous offre|offre accept[ée]e)\b", re.I
)
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scraper")
 
 
# --------------------------------------------------------------------------- #
# MODÈLE
# --------------------------------------------------------------------------- #
@dataclass
class Annonce:
    url: str
    agence: str
    titre: str = ""
    prix: int | None = None
    surface: float | None = None
    terrain: float | None = None
    prix_m2: int | None = None
    score: int = 0
    mots_detectes: str = ""
    nouvelle: bool = False
    date_collecte: str = ""
    localisation: str = ""
    v: int = VERSION_PARSEUR
 
 
# --------------------------------------------------------------------------- #
# OUTILS
# --------------------------------------------------------------------------- #
def normaliser_url(url: str) -> str:
    url, _ = urldefrag(url)
    return url.rstrip("/")
 
 
def pause(a: float = 0.8, b: float = 2.0) -> None:
    import time
    time.sleep(random.uniform(a, b))
 
 
def accepter_cookies(page) -> None:
    """Clique sur le bandeau cookies (bouton OU lien, ex. JMB utilise un lien « Accepter »)."""
    motif = re.compile(r"^\s*(tout accepter|accepter|j.accepte|ok|continuer sans accepter)\s*$", re.I)
    try:
        btn = page.locator("button, a, input[type=button]").filter(has_text=motif).first
        if btn.count() and btn.is_visible(timeout=1000):
            btn.click(timeout=2000)
            page.wait_for_timeout(500)
    except Exception:
        pass
 
 
JS_NB_PAR_PAGE = """() => {
  // Liste « nb par page » (options numériques + « Tous ») -> on choisit « Tous »
  document.querySelectorAll('select').forEach(sel => {
    const opts = [...sel.options];
    const tous = opts.find(o => /^\\s*(tous|tout|all)\\s*$/i.test(o.text));
    const autres = opts.filter(o => o !== tous);
    if (tous && autres.length && autres.every(o => /^\\s*\\d+\\s*$/.test(o.text)) && sel.value !== tous.value) {
      sel.value = tous.value;
      sel.dispatchEvent(new Event('change', {bubbles: true}));
    }
  });
}"""
 
 
def afficher_tout(page) -> None:
    """Force l'affichage de tous les biens : « nb par page = Tous » + boutons « Voir plus »."""
    try:
        page.evaluate(JS_NB_PAR_PAGE)
        page.wait_for_timeout(1500)
    except Exception:
        pass
    motif = re.compile(r"(voir|charger|afficher) (plus|davantage|la suite)|plus (de biens|de résultats|d.annonces)", re.I)
    for _ in range(10):
        try:
            btn = page.locator("button, a").filter(has_text=motif).first
            if btn.count() and btn.is_visible(timeout=500):
                btn.click(timeout=2000)
                page.wait_for_timeout(1200)
            else:
                break
        except Exception:
            break
 
 
def sauver_debug(page, nom: str) -> None:
    """Sauvegarde HTML + capture d'écran quand aucune fiche n'est trouvée (pour diagnostic)."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        slug = re.sub(r"\W+", "_", nom.lower()).strip("_")
        (DEBUG_DIR / f"{slug}.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(DEBUG_DIR / f"{slug}.png"), full_page=True)
        log.warning("   📸 debug sauvegardé dans %s/%s.*", DEBUG_DIR, slug)
    except Exception as e:
        log.warning("   debug impossible : %s", e)
 
 
def charger_page(page, url: str, tentatives: int = 3) -> bool:
    """Charge une page avec retry. 'domcontentloaded' évite les blocages 'networkidle'."""
    for i in range(1, tentatives + 1):
        try:
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PWTimeout:
                pass  # certains sites ne se calment jamais (pubs, trackers)
            return True
        except Exception as e:
            log.warning("Tentative %d/%d échouée pour %s : %s", i, tentatives, url, e)
            pause(2, 4)
    return False
 
 
def scroller_jusqu_au_bout(page, max_tours: int = 8) -> None:
    """Scroll tant que la hauteur augmente (lazy-loading / infinite scroll)."""
    derniere = 0
    for _ in range(max_tours):
        hauteur = page.evaluate("document.body.scrollHeight")
        if hauteur == derniere:
            break
        derniere = hauteur
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(900)
 
 
# Détection « carte d'annonce » : un lien dont le bloc parent (≤ 5 niveaux) affiche un prix en €.
# Indépendant de la structure d'URL -> fonctionne même quand le motif d'URL est inconnu.
JS_CARTES_PRIX = """() => {
  const out = new Set();
  document.querySelectorAll('a[href]').forEach(a => {
    let el = a;
    for (let i = 0; i < 5 && el; i++, el = el.parentElement) {
      const t = el.innerText || '';
      if (t.length > 1500) break;                         // conteneur trop large (toute la grille)
      if (/\\d[\\d\\s.\\u00a0\\u202f]*(€|euros?)/i.test(t)) { out.add(a.href); break; }
    }
  });
  return [...out];
}"""
 
 
def _domaine(url: str) -> str:
    """Domaine sans 'www.' (évite de rejeter mesnard-immobilier.com vs www.mesnard-immobilier.com)."""
    return urlparse(url).netloc.lower().removeprefix("www.")
 
 
def _filtrer_liens(hrefs: list[str], base_url: str) -> set[str]:
    domaine, base = _domaine(base_url), normaliser_url(base_url)
    liens = set()
    for href in hrefs:
        href = normaliser_url(href)
        if urlparse(href).scheme not in ("http", "https"):
            continue
        if _domaine(href) != domaine or href == base or EXCLUS.search(href):
            continue
        liens.add(href)
    return liens
 
 
def extraire_liens_fiches(page, base_url: str, pattern: re.Pattern, mode: str = "auto") -> set[str]:
    """
    mode "pattern" : liens dont l'URL correspond au motif
    mode "prix"    : liens situés dans une carte qui affiche un prix (indépendant des URLs)
    mode "auto"    : motif d'abord, repli sur « prix » si le motif ne trouve rien
    """
    # .href renvoie déjà l'URL ABSOLUE (corrige les liens relatifs mal résolus)
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    par_pattern = {
        h for h in _filtrer_liens(hrefs, base_url)
        if pattern.search(urlparse(h).path + (f"?{urlparse(h).query}" if urlparse(h).query else ""))
    }
    if mode == "pattern":
        return par_pattern
    par_prix = _filtrer_liens(page.evaluate(JS_CARTES_PRIX), base_url)
    if mode == "prix":
        return par_prix
    return par_pattern or par_prix
 
 
def aller_page_suivante(page) -> bool:
    selecteurs = [
        "a[rel='next']",
        "a.next", "li.next a", ".pagination a.next",
        "a:has-text('Suivant')", "a:has-text('suivant')", "a:has-text('›')", "a:has-text('»')",
    ]
    for sel in selecteurs:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible(timeout=500):
                el.click(timeout=3000)
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False
 
 
# --------------------------------------------------------------------------- #
# 1) COLLECTE DES LIENS
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# FILTRE « VILLE » PILOTÉ PAR L'INTERFACE (sites de recherche en JavaScript, ex. notaires)
# --------------------------------------------------------------------------- #
SELECTEURS_CHAMP_VILLE = [
    'input[placeholder*="ville" i]', 'input[placeholder*="localisation" i]', 'input[placeholder*="où" i]',
    'input[placeholder*="code postal" i]', 'input[aria-label*="ville" i]', 'input[aria-label*="localisation" i]',
    'input[aria-label*="lieu" i]', 'input[name*="locali" i]', 'input[id*="locali" i]',
    'input[name*="ville" i]', 'input[id*="ville" i]', 'input[type="search"]', 'input[role="combobox"]',
]
SELECTEURS_SUGGESTIONS = [
    '[role="option"]', '[role="listbox"] li', 'li[class*="suggest" i]', '[class*="autocomplete" i] li',
    '[class*="suggestion" i]', 'li',
]
SELECTEURS_VALIDER = [
    'button:has-text("Rechercher")', 'button:has-text("Lancer la recherche")',
    '[role="button"]:has-text("Rechercher")', 'button[type="submit"]',
]
 
 
def filtrer_par_ville(page, ville: str = "Millau") -> bool:
    """
    Saisit la ville dans le champ de recherche, choisit la suggestion « Millau (12…) » et valide.
    Retourne True si la recherche a pu être lancée. Écrit l'URL filtrée dans le log.
    """
    try:
        champ = None
        for sel in SELECTEURS_CHAMP_VILLE:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=1200):
                champ = loc
                break
        if champ is None:
            log.warning("   champ « ville » introuvable")
            return False
 
        champ.click(timeout=3000)
        champ.fill("")
        champ.press_sequentially(ville, delay=120)      # frappe lente : déclenche l'auto-complétion
        page.wait_for_timeout(1800)
 
        motif = re.compile(re.escape(ville), re.I)
        prefere = re.compile(r"\b12\b|12100|aveyron", re.I)
        choisi = False
        for sel in SELECTEURS_SUGGESTIONS:
            sugg = page.locator(sel).filter(has_text=motif)
            if not sugg.count():
                continue
            cible = sugg.filter(has_text=prefere)
            cible = (cible if cible.count() else sugg).first
            if cible.is_visible(timeout=800):
                cible.click(timeout=3000)
                choisi = True
                break
        if not choisi:
            log.warning("   aucune suggestion « %s » trouvée (on valide avec la saisie brute)", ville)
 
        page.wait_for_timeout(600)
        for sel in SELECTEURS_VALIDER:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible(timeout=800):
                btn.click(timeout=3000)
                break
        else:
            champ.press("Enter")
 
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass
        page.wait_for_timeout(2000)
        log.info("   🔎 URL filtrée (à copier dans AGENCES pour figer le filtre) : %s", page.url)
        return True
    except Exception as e:
        log.warning("   filtre « %s » en échec : %s", ville, e)
        return False
 
 
PREPARATIONS = {"filtre_ville": filtrer_par_ville}
 
 
def get_liens_agences_locales(context) -> dict[str, str]:
    """Retourne {url_fiche: nom_agence} pour toutes les agences configurées."""
    resultats: dict[str, str] = {}
    page = context.new_page()
 
    for agence in AGENCES:
        nom, url = agence["nom"], agence["url"]
        pattern = re.compile(agence.get("pattern_fiche", PATTERN_FICHE_DEFAUT), re.I)
        log.info("▶ %s : %s", nom, url)
 
        if not charger_page(page, url):
            log.error("Abandon de %s", nom)
            continue
        mode = agence.get("mode", "auto")
        accepter_cookies(page)
 
        prep = agence.get("preparation")
        if prep and not PREPARATIONS[prep](page, agence.get("ville", "Millau")):
            log.error("   ✗ filtre « ville » impossible : %s ignoré pour ce run (évite de scanner toute la France)", nom)
            sauver_debug(page, f"{nom}_filtre")
            continue
 
        total_agence = 0
        for num_page in range(1, MAX_PAGES_PAR_AGENCE + 1):
            afficher_tout(page)
            scroller_jusqu_au_bout(page)
            nouveaux = extraire_liens_fiches(page, url, pattern, mode) - set(resultats)
            for lien in nouveaux:
                resultats[lien] = nom
            total_agence += len(nouveaux)
            log.info("   page %d : +%d liens", num_page, len(nouveaux))
            if nouveaux:
                log.info("   exemples : %s", sorted(nouveaux)[:2])
            elif num_page == 1:
                sauver_debug(page, nom)
 
            if not nouveaux and num_page > 1:
                break  # la pagination ne ramène plus rien
            if not aller_page_suivante(page):
                break
            pause()
 
        log.info("   → %d fiches pour %s", total_agence, nom)
        pause()
 
    page.close()
    return resultats
 
 
# --------------------------------------------------------------------------- #
# 2) EXTRACTION D'UNE FICHE
# --------------------------------------------------------------------------- #
# NB : l'ancienne regex finissait par « \b » après « € ». Or il n'y a PAS de frontière de mot entre
# « € » et un espace -> « 95 000 € » n'était jamais reconnu. Corrigé ci-dessous.
_NUM = r"\d{1,3}(?:[ \u00a0\u202f.,]\d{3})+|\d{4,8}"
RE_PRIX = re.compile(
    rf"(?:€\s*({_NUM})(?![\d])"                                  # « € 95 000 » / « €95,000 »
    rf"|({_NUM})(?:[.,]\d{{1,2}})?\s*(?:€|euros?\b|eur\b))",     # « 95 000 € » / « 95000€ » / « 95 000 euros »
    re.I,
)
CTX_POSITIF = re.compile(r"prix|vente|f\.?a\.?i\b|h\.?a\.?i\b|net vendeur|à vendre", re.I)
CTX_NEGATIF = re.compile(
    r"honoraires?|frais|charges?|taxe|fonci[èe]re|d[ée]p[ôo]t|loyer|par mois|/\s*mois|commission|copropri",
    re.I,
)
RE_PRIX_META = re.compile(
    r'(?:"price"\s*:\s*"?|price:amount"\s+content="|itemprop="price"\s+content=")(\d{4,8})(?:\.\d+)?', re.I
)
 
 
def extraire_prix(texte: str, html: str = "") -> int | None:
    """
    1) Parcourt tous les montants en € du texte (> 10 k€), favorise ceux proches de « prix / vente / FAI »
       et pénalise « honoraires / charges / loyer / taxe foncière ».
    2) À défaut, lit les métadonnées de la page (JSON-LD, og:price, itemprop=price).
    """
    candidats = []
    for m in RE_PRIX.finditer(texte):
        val = int(re.sub(r"\D", "", m.group(1) or m.group(2)))
        if not 10_000 <= val <= 5_000_000:
            continue
        ctx = texte[max(0, m.start() - 50): m.end() + 30]
        pts = (2 if CTX_POSITIF.search(ctx) else 0) - (3 if CTX_NEGATIF.search(ctx) else 0)
        candidats.append((pts, -m.start(), val))   # meilleur score, puis le plus haut dans la page
    if candidats:
        return max(candidats)[2]
 
    for m in RE_PRIX_META.finditer(html):
        val = int(m.group(1))
        if 10_000 <= val <= 5_000_000:
            return val
    return None
 
 
RE_SURFACE = re.compile(r"(\d{2,4}(?:[.,]\d{1,2})?)\s*m(?:²|2)\b", re.I)
RE_TERRAIN = re.compile(r"(?:terrain|parcelle|jardin)[^\d]{0,40}(\d{2,6}(?:[.,]\d{1,2})?)\s*m(?:²|2)", re.I)
 
 
class PageListe(Exception):
    """La page est une liste de résultats (pas une fiche). `liens` = fiches à explorer ensuite."""
    def __init__(self, liens=()):
        super().__init__("page de liste")
        self.liens = set(liens)
 
 
# « 36 458 annonces… », « 13 BIENS TROUVÉS », « 9 APPARTEMENTS TROUVÉS », « TOUS TYPES DE BIENS »…
RE_PAGE_LISTE = re.compile(
    r"^\s*\d[\d\s\u00a0\u202f]*\s+(?:annonces?|biens?|appartements?|maisons?|terrains?|programmes?|r[ée]sultats?|offres?)\b"
    r"|\btrouv[ée]e?s?\b|\btous types\b|\bnos (?:biens|annonces|offres)\b|\ben location\b",
    re.I,
)
# Référence de fin d'URL type ICS/JMB : « …-veyreau-TAPP100088 » -> commune = « veyreau »
RE_SLUG_COMMUNE = re.compile(r"-([a-zà-ÿ_]{3,})-[a-z]{3,5}\d{4,}/?$", re.I)
# Coupe le texte avant les carrousels « biens similaires » (qui polluent prix et mots-clés)
RE_SIMILAIRES = re.compile(
    r"biens? similaires|annonces? similaires|vous aimerez (?:aussi|également)|autres (?:biens|annonces)|"
    r"d.autres (?:biens|annonces)",
    re.I,
)
 
 
def titre_depuis_url(url: str) -> str:
    """JMB n'a ni <h1> ni <title> exploitable : on fabrique un titre lisible depuis l'URL."""
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return re.sub(r"[-_]+", " ", slug).strip().capitalize() or url
 
 
class FicheInjoignable(Exception):
    """Page impossible à charger : on ne mémorise pas la fiche, elle sera retentée au prochain run."""
 
 
# Un code postal suivi d'un nom de commune : « 12100 Millau », « 12230 Nant »…
# (le (?!euros?) évite de confondre « 95000 euros » avec un code postal)
RE_CP_VILLE = re.compile(r"\b(\d{5})[\s,\-]+(?!euros?\b|eur\b)([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’\-]+)", re.I)
 
# Texte de la fiche SANS en-tête / pied de page / menus (le pied de page des agences de Millau
# contient « 12100 Millau » sur toutes leurs pages : il fausserait le filtre).
JS_CONTENU = """() => {
  const cands = [...document.querySelectorAll('main, article, #content, .content')];
  const root = cands.sort((a, b) => b.textContent.length - a.textContent.length)[0] || document.body;
  const c = root.cloneNode(true);
  c.querySelectorAll('header, footer, nav, aside, script, style, [class*=footer], [class*=header], [id*=footer], [id*=header], [class*=cookie], [class*=menu]')
    .forEach(e => e.remove());
  return (c.textContent || '').replace(/\\s+/g, ' ').trim();
}"""
 
 
def _est_cible(texte: str) -> bool:
    return any(v in texte.lower() for v in VILLES_AUTORISEES)
 
 
# Mots qui suivent parfois un nombre à 5 chiffres sans être une commune (« réf 10484 Appartement T4 »)
MOTS_NON_COMMUNE = {
    "appartement", "appartements", "maison", "maisons", "villa", "immeuble", "terrain", "studio",
    "local", "duplex", "loft", "garage", "parking", "bien", "annonce", "vente", "location", "ref",
    "reference", "mandat", "type", "piece", "pieces", "chambre", "chambres", "euros", "eur",
    "annonce", "immeuble", "local", "fonds",
}
RE_REF_AVANT = re.compile(r"(r[ée]f|r[ée]f[ée]rence|mandat|n°|n°\s|id|code|dpe|annonce)\W{0,3}$", re.I)
# Code postal aveyronnais (12xxx), sauf s'il s'agit d'un prix (« 12500 € »)
RE_CP_AVEYRON = re.compile(r"\b(12\d{3})\b(?!\s*(?:€|euros?\b|eur\b))", re.I)
 
 
def _adresses(texte: str) -> list[tuple[str, str]]:
    """Couples (code postal, commune) réellement plausibles : « 12100 Millau », « 12230 Nant »."""
    trouves = []
    for m in RE_CP_VILLE.finditer(texte):
        cp, ville = m.group(1), m.group(2)
        if not ("01" <= cp[:2] <= "95"):
            continue
        if ville.lower().strip("-’'") in MOTS_NON_COMMUNE or ville.lower().startswith(("appartement", "maison")):
            continue
        if RE_REF_AVANT.search(texte[max(0, m.start() - 12): m.start()]):
            continue
        trouves.append((cp, ville))
    return trouves
 
 
def evaluer_localisation(url: str, h1: str, contenu: str) -> str:
    """
    Retourne « oui » (Millau confirmé), « non » (autre commune détectée) ou « incertain ».
 
    On n'utilise PAS le <title> ni la meta description : pour une agence basée à Millau (JMB, Roques…),
    ils contiennent « Millau » sur toutes les pages, même pour un bien situé ailleurs.
    Ordre de fiabilité : chemin de l'URL > titre H1 > début du contenu (sans en-tête/pied de page).
    """
    # 1) Le chemin de l'URL est propre à l'annonce (ex. /vente/435-millau/appartement/…)
    if _est_cible(urlparse(url).path):
        return "oui"
 
    # 1 bis) Slug de type « …-<commune>-<REF> » : commune différente de Millau => hors zone
    m_slug = RE_SLUG_COMMUNE.search(urlparse(url).path)
    if m_slug:
        commune = m_slug.group(1).replace("_", " ").lower()
        if commune not in MOTS_NON_COMMUNE:
            return "oui" if _est_cible(commune) else "non"
 
    # 2) Titre de l'annonce
    adr_h1 = _adresses(h1)
    if any(_est_cible(v) for _, v in adr_h1):
        return "oui"
    cp_autres_h1 = set(RE_CP_AVEYRON.findall(h1)) - {"12100"}
    if adr_h1 or cp_autres_h1:
        return "non"                       # autre commune annoncée dans le titre
    if _est_cible(h1):
        return "oui"
 
    # 3) Début du contenu de la fiche
    adr = _adresses(contenu[:3000])
    if any(_est_cible(v) for _, v in adr):
        return "oui"
    if adr:
        return "non"
    return "incertain"                     # rien de décisif -> gardée, signalée « à vérifier »
 
 
_NB_DEBUG_FICHES = 0
 
 
def analyser_fiche(page, url: str, agence: str) -> Annonce | None:
    if not charger_page(page, url, tentatives=2):
        raise FicheInjoignable(url)
 
    # Beaucoup de sites affichent le prix en JavaScript après le chargement : on l'attend
    try:
        page.wait_for_function(
            "document.body && /€|euro/i.test(document.body.innerText)", timeout=6000
        )
    except PWTimeout:
        pass
 
    try:
        titre = h1 = page.locator("h1").first.inner_text(timeout=3000).strip()
    except Exception:
        titre, h1 = page.title(), ""
 
    texte = page.inner_text("body")
    contenu = page.evaluate(JS_CONTENU)
    coupe = RE_SIMILAIRES.search(contenu)
    if coupe and coupe.start() > 300:
        contenu = contenu[:coupe.start()]
    if not titre or "://" in titre:
        titre = titre_depuis_url(url)
 
    # Page de liste (résultats de recherche) et non fiche : on ne l'enregistre pas comme annonce.
    # Si elle concerne Millau, on en récupère les fiches pour les analyser ensuite.
    if RE_PAGE_LISTE.search(f"{h1} {page.title()}"):
        liens = set()
        if _est_cible(f"{h1} {urlparse(url).path}"):
            liens = _filtrer_liens(page.evaluate(JS_CARTES_PRIX), url)
        log.info("   ↪ page de liste (%d fiches à explorer) : %s", len(liens), url)
        raise PageListe(liens)
 
    m_exclu = MOTS_REDHIBITOIRES.search(f"{h1} {contenu[:2000]}")
    if m_exclu:
        log.info("   ✗ écartée (« %s ») : %s", m_exclu.group(0), url)
        return None
 
    # Filtre géographique : Millau uniquement
    statut = evaluer_localisation(url, h1, contenu)
    if statut == "non":
        log.info("   ✗ hors Millau : %s", url)
        return None
    localisation = "Millau" if statut == "oui" else "à vérifier"
 
    # Le contenu « nettoyé » (sans menu/pied de page) évite les faux mots-clés et faux prix
    base = contenu if len(contenu) >= 300 else texte
    prix = extraire_prix(base) or extraire_prix(texte, page.content())
    if prix is None:
        global _NB_DEBUG_FICHES
        log.warning("   ⚠ prix introuvable : %s", url)
        if _NB_DEBUG_FICHES < 3:           # on garde 3 fiches pour diagnostic
            _NB_DEBUG_FICHES += 1
            sauver_debug(page, f"fiche_sans_prix_{_NB_DEBUG_FICHES}_{agence}")
 
    surfaces = [float(s.replace(",", ".")) for s in RE_SURFACE.findall(base)]
    surface = next((s for s in surfaces if 15 <= s <= 1500), None)
    m_terrain = RE_TERRAIN.search(base)
    terrain = float(m_terrain.group(1).replace(",", ".")) if m_terrain else None
 
    # Scoring
    score, detectes = 0, []
    texte_bas = f"{titre} {base}".lower()
    for regex, points in MOTS_CLES.items():
        m = re.search(regex, texte_bas, re.I)
        if m:
            score += points
            detectes.append(m.group(0).strip())
 
    prix_m2 = int(prix / surface) if prix and surface else None
    if prix_m2 and prix_m2 < 1200:  # bonus : prix au m² très bas pour le secteur
        score += 2
        detectes.append(f"{prix_m2} €/m²")
 
    return Annonce(
        url=url, agence=agence, titre=titre[:150], prix=prix, surface=surface,
        terrain=terrain, prix_m2=prix_m2, score=score,
        mots_detectes=", ".join(dict.fromkeys(detectes)),
        date_collecte=datetime.now().strftime("%Y-%m-%d %H:%M"),
        localisation=localisation,
    )
 
 
# --------------------------------------------------------------------------- #
# 3) ÉTAT & EXPORT
# --------------------------------------------------------------------------- #
def charger_etat() -> dict:
    if FICHIER_ETAT.exists():
        return json.loads(FICHIER_ETAT.read_text(encoding="utf-8"))
    return {}
 
 
def sauver_etat(etat: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    FICHIER_ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=1), encoding="utf-8")
 
 
def exporter_csv(annonces: list[Annonce]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    if not annonces:
        return
    with FICHIER_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(annonces[0])), delimiter=";")
        w.writeheader()
        w.writerows(asdict(a) for a in annonces)
 
 
# --------------------------------------------------------------------------- #
# 4) RAPPORT HTML (GitHub Pages)
# --------------------------------------------------------------------------- #
TEMPLATE_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Veille immobilière – Millau (Aveyron)</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --txt:#1c2330; --mut:#6b7585; --bd:#e3e6ec; --acc:#1f6feb; --new:#d9480f; --ok:#2b8a3e; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0f1218; --card:#171b24; --txt:#e6e9ef; --mut:#8b95a7; --bd:#262c3a; --acc:#58a6ff; --new:#ff8c5a; --ok:#51cf66; }
  }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--txt); }
  header { padding:24px 20px 8px; max-width:1200px; margin:auto; }
  h1 { margin:0 0 4px; font-size:22px; }
  .sub { color:var(--mut); font-size:13px; }
  .kpis { display:flex; gap:12px; flex-wrap:wrap; max-width:1200px; margin:12px auto; padding:0 20px; }
  .kpi { background:var(--card); border:1px solid var(--bd); border-radius:10px; padding:10px 16px; }
  .kpi b { display:block; font-size:20px; }
  .kpi span { color:var(--mut); font-size:12px; }
  .filters { max-width:1200px; margin:8px auto; padding:0 20px; display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  .filters input, .filters select { padding:7px 10px; border:1px solid var(--bd); border-radius:8px; background:var(--card); color:var(--txt); font:inherit; }
  .filters label { color:var(--mut); font-size:13px; display:flex; gap:6px; align-items:center; }
  .wrap { max-width:1200px; margin:8px auto 40px; padding:0 20px; overflow-x:auto; }
  table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--bd); border-radius:10px; overflow:hidden; }
  th, td { padding:9px 12px; text-align:left; border-bottom:1px solid var(--bd); white-space:nowrap; }
  td.wrap-txt { white-space:normal; min-width:260px; }
  th { cursor:pointer; user-select:none; font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--mut); }
  th:hover { color:var(--txt); }
  tr:last-child td { border-bottom:0; }
  a { color:var(--acc); text-decoration:none; } a:hover { text-decoration:underline; }
  .badge { display:inline-block; padding:1px 8px; border-radius:99px; font-size:11px; font-weight:600; }
  .b-new { background:color-mix(in srgb, var(--new) 18%, transparent); color:var(--new); }
  .score { font-weight:700; }
  .s-hi { color:var(--ok); } .s-mid { color:var(--acc); } .s-lo { color:var(--mut); }
  .kw { color:var(--mut); font-size:12px; }
  .empty { text-align:center; color:var(--mut); padding:30px; }
</style>
</head>
<body>
<header>
  <h1>Veille immobilière – secteur Millau</h1>
  <div class="sub">Mise à jour : __DATE__</div>
</header>
<div class="kpis" id="kpis"></div>
<div class="filters">
  <input id="q" type="search" placeholder="Rechercher (titre, mots-clés)…">
  <select id="agence"><option value="">Toutes les agences</option></select>
  <label>Score min <input id="smin" type="number" min="0" value="0" style="width:64px"></label>
  <label>Prix max <input id="pmax" type="number" step="10000" placeholder="€" style="width:100px"></label>
  <label><input id="nouv" type="checkbox"> Nouvelles uniquement</label>
  <label><input id="millau" type="checkbox"> Millau confirmé</label>
</div>
<div class="wrap">
  <table>
    <thead><tr id="head"></tr></thead>
    <tbody id="body"></tbody>
  </table>
</div>
<script>
const DATA = __DATA__;
const COLS = [
  {k:"score", t:"Score"}, {k:"titre", t:"Annonce"}, {k:"agence", t:"Agence"},
  {k:"prix", t:"Prix"}, {k:"surface", t:"Surface"}, {k:"terrain", t:"Terrain"},
  {k:"prix_m2", t:"€/m²"}, {k:"mots_detectes", t:"Signaux"}
];
let sortKey = "score", sortDir = -1;
const $ = id => document.getElementById(id);
const fmt = (v, suf="") => v == null || v === "" ? "–" : Number(v).toLocaleString("fr-FR") + suf;
 
[...new Set(DATA.map(a => a.agence))].sort().forEach(n => {
  const o = document.createElement("option"); o.value = o.textContent = n; $("agence").appendChild(o);
});
COLS.forEach(c => {
  const th = document.createElement("th"); th.textContent = c.t;
  th.onclick = () => { sortDir = sortKey === c.k ? -sortDir : -1; sortKey = c.k; render(); };
  $("head").appendChild(th);
});
 
function cell(tr, text, cls) { const td = document.createElement("td"); td.textContent = text; if (cls) td.className = cls; tr.appendChild(td); return td; }
 
function render() {
  const q = $("q").value.toLowerCase(), ag = $("agence").value;
  const smin = +$("smin").value || 0, pmax = +$("pmax").value || Infinity, nouv = $("nouv").checked;
  const rows = DATA.filter(a =>
    (!ag || a.agence === ag) && a.score >= smin && (a.prix == null || a.prix <= pmax) &&
    (!nouv || a.nouvelle) && (!$("millau").checked || a.localisation === "Millau") && (!q || (a.titre + " " + a.mots_detectes).toLowerCase().includes(q))
  ).sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    if (x == null) return 1; if (y == null) return -1;
    return (x > y ? 1 : x < y ? -1 : 0) * sortDir;
  });
 
  $("kpis").innerHTML = "";
  [[DATA.length, "annonces suivies"], [DATA.filter(a => a.nouvelle).length, "nouvelles"],
   [DATA.filter(a => a.score >= 4).length, "score ≥ 4"], [rows.length, "affichées"]].forEach(([n, l]) => {
    const d = document.createElement("div"); d.className = "kpi";
    d.innerHTML = "<b></b><span></span>"; d.children[0].textContent = n; d.children[1].textContent = l;
    $("kpis").appendChild(d);
  });
 
  const body = $("body"); body.innerHTML = "";
  if (!rows.length) { body.innerHTML = '<tr><td class="empty" colspan="8">Aucune annonce ne correspond.</td></tr>'; return; }
  rows.forEach(a => {
    const tr = document.createElement("tr");
    cell(tr, a.score, "score " + (a.score >= 5 ? "s-hi" : a.score >= 3 ? "s-mid" : "s-lo"));
    const td = cell(tr, "", "wrap-txt");
    const link = document.createElement("a"); link.href = a.url; link.target = "_blank"; link.rel = "noopener";
    link.textContent = a.titre || a.url; td.appendChild(link);
    if (a.nouvelle) { const b = document.createElement("span"); b.className = "badge b-new"; b.textContent = "NOUVEAU"; b.style.marginLeft = "8px"; td.appendChild(b); }
    if (a.localisation !== "Millau") { const v = document.createElement("span"); v.className = "badge"; v.textContent = "lieu à vérifier"; v.style.marginLeft = "8px"; v.style.background = "var(--bd)"; td.appendChild(v); }
    cell(tr, a.agence);
    cell(tr, fmt(a.prix, " €")); cell(tr, fmt(a.surface, " m²")); cell(tr, fmt(a.terrain, " m²")); cell(tr, fmt(a.prix_m2, " €"));
    cell(tr, a.mots_detectes, "kw wrap-txt");
    body.appendChild(tr);
  });
}
["q","agence","smin","pmax","nouv","millau"].forEach(id => $(id).addEventListener("input", render));
render();
</script>
</body>
</html>
"""
 
 
def generer_rapport_html(annonces: list[Annonce]) -> None:
    """Génère docs/index.html : page autonome (tri, filtres) pour GitHub Pages."""
    DOSSIER_DOCS.mkdir(exist_ok=True)
    donnees = json.dumps([asdict(a) for a in annonces], ensure_ascii=False)
    donnees = donnees.replace("</", "<\\/")  # évite de fermer la balise <script>
    contenu = (
        TEMPLATE_HTML
        .replace("__DATA__", donnees)
        .replace("__DATE__", datetime.now().strftime("%d/%m/%Y à %H:%M"))
    )
    FICHIER_HTML.write_text(contenu, encoding="utf-8")
    (DOSSIER_DOCS / ".nojekyll").touch()  # désactive Jekyll sur GitHub Pages
    log.info("📄 Rapport HTML généré : %s", FICHIER_HTML)
 
 
# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #
def main() -> None:
    etat = charger_etat()  # {url: annonce_dict}
 
    traitees: set[str] = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="fr-FR")
            # Pas d'images/polices : scraping 2 à 3x plus rapide
            context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("image", "font", "media")
                else route.continue_(),
            )
 
            liens = get_liens_agences_locales(context)
            log.info("✨ %d liens de fiches collectés", len(liens))
 
            # On analyse les fiches inconnues, puis celles vues avec un ancien parseur (VERSION_PARSEUR)
            inconnues = [(u, a) for u, a in liens.items() if u not in etat]
            obsoletes = [(u, a) for u, a in liens.items()
                         if u in etat and etat[u].get("v") != VERSION_PARSEUR]
            a_traiter = (inconnues + obsoletes)[:MAX_FICHES_PAR_RUN]
            log.info("🆕 %d nouvelles fiches + %d à ré-analyser", len(inconnues), len(obsoletes))
 
            page = context.new_page()
            file = deque(a_traiter)
            en_file = {u for u, _ in a_traiter}
            depuis_liste: set[str] = set()      # fiches découvertes via une page de liste (profondeur 1)
            nb = 0
            while file and nb < MAX_FICHES_PAR_RUN:
                url, agence = file.popleft()
                nb += 1
                traitees.add(url)
                log.info("[%d] %s", nb, url)
                deja_connue = url in etat
                try:
                    annonce = analyser_fiche(page, url, agence)
                except FicheInjoignable:
                    log.warning("   ⚠ fiche injoignable, retentée au prochain run")
                    continue
                except PageListe as pl:
                    etat[url] = {"url": url, "ignoree": True, "v": VERSION_PARSEUR}
                    if url not in depuis_liste:             # pas de récursion au-delà d'un niveau
                        for lien in pl.liens:
                            if lien not in en_file and etat.get(lien, {}).get("v") != VERSION_PARSEUR:
                                file.append((lien, agence))
                                en_file.add(lien)
                                depuis_liste.add(lien)
                    pause()
                    continue
                if annonce:
                    annonce.nouvelle = not deja_connue   # une ré-analyse ne la re-marque pas « nouvelle »
                    etat[url] = asdict(annonce)
                else:
                    etat[url] = {"url": url, "ignoree": True, "v": VERSION_PARSEUR}
                pause()
        finally:
            browser.close()
 
    # Marque comme "plus nouvelles" celles déjà connues
    sauver_etat({u: {**d, "nouvelle": d.get("nouvelle", False) and u in traitees}
                 for u, d in etat.items()})
 
    # seules les fiches analysées avec le parseur/filtre courant sont publiées
    annonces = [Annonce(**d) for d in etat.values()
                if not d.get("ignoree") and d.get("v") == VERSION_PARSEUR]
    annonces = [a for a in annonces if PRIX_MAX is None or a.prix is None or a.prix <= PRIX_MAX]
    annonces.sort(key=lambda a: (a.nouvelle, a.score), reverse=True)
    exporter_csv(annonces)
    generer_rapport_html(annonces)
 
    interessantes = [a for a in annonces if a.nouvelle and a.score >= SCORE_MINIMUM]
    print(f"\n🔥 {len(interessantes)} nouvelle(s) annonce(s) intéressante(s) :\n")
    for a in interessantes:
        prix = f"{a.prix:,} €".replace(",", " ") if a.prix else "prix ?"
        print(f"[{a.score}] {a.agence} | {prix} | {a.surface or '?'} m² | {a.titre}")
        print(f"    {a.mots_detectes}\n    {a.url}\n")
 
 
if __name__ == "__main__":
    main()
