#!/usr/bin/env python3
"""
Veille immobilière pour marchand de biens (secteur Millau).

Pipeline :
  1. Collecte des liens de fiches sur les catalogues des agences (avec pagination)
  2. Extraction des infos de chaque fiche (prix, surface, titre, description)
  3. Scoring "marchand de biens" (mots-clés travaux / division / succession / immeubles, prix au m²)
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

# Zone géographique : uniquement Millau (Aveyron).
VILLES_AUTORISEES = ("millau",)

PRIX_MAX = 250_000          # budget maximum (None pour désactiver)
SCORE_MINIMUM = 2           # seuil pour être jugée "intéressante"
MAX_PAGES_PAR_AGENCE = 5    # pagination
MAX_FICHES_PAR_RUN = 200    # garde-fou
VERSION_PARSEUR = 8         # V8 : exclusion des locations + correctif de capture des prix
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

PATTERN_FICHE_DEFAUT = (
    r"(/annonce|/bien/|/vente/.+|/detail|/property/|/fiche|/produit|ref-|/\d{4,}|-r\d+)"
)

AGENCES = [
    {
        "nom": "Roques",
        "url": "https://www.roques-immobilier.com/vente/435-millau/1",
        "pattern_fiche": r"/vente/435-millau/[^/]+/\d+-",
        "mode": "pattern",
    },
    {"nom": "SGA", "url": "https://www.sga-immobilier.com/immobilier/immobilier-vente-millau.htm"},
    {"nom": "JMB", "url": "https://www.jmb-immobilier.com/resultats?transac=vente", "mode": "prix"},
    {
        "nom": "Notaires",
        "url": "https://www.immobilier.notaires.fr/fr/annonces-immobilieres-liste?typeTransaction=VENTE,VNI,VAE",
        "mode": "prix",
        "preparation": "filtre_ville",
        "ville": "Millau",
    },
    {"nom": "Mesnard maisons", "url": "https://mesnard-immobilier.com/property-type/maison/", "pattern_fiche": r"/property/"},
    {"nom": "Mesnard apparts", "url": "https://mesnard-immobilier.com/property-type/appartement/", "pattern_fiche": r"/property/"},
    {"nom": "Mesnard immeubles", "url": "https://mesnard-immobilier.com/property-type/immeuble/", "pattern_fiche": r"/property/"},
    {"nom": "AP Immobilier", "url": "https://www.apimmobilier.fr/recherche/"},
]

# Motif d'exclusion : ajout explicite des URLs de location et de gestion
EXCLUS = re.compile(
    r"(contact|mentions|legal|cgu|cgv|confidential|cookies|estimation|connexion|login|"
    r"inscri|actualit|blog|recrut|honoraires|facebook|instagram|linkedin|twitter|"
    r"youtube|mailto:|tel:|javascript:|\.pdf$|\.jpg$\vert{}\.png$|/location/|/louer/|/a-louer/)",
    re.I,
)

# --------------------------------------------------------------------------- #
# BARÈME DE SCORING
# --------------------------------------------------------------------------- #
MOTS_CLES = {
    # ÉTAT DU BIEN : Gros travaux / rénovation lourde (+5 points)
    r"\b(à|a) r[ée]nover\b|\bà restaurer\b|\br[ée]novation (totale|compl[èe]te)\b|\bgros [œo]uvre\b|\bplateau [àa] am[ée]nager\b": 5,
    # ÉTAT DU BIEN : Rafraîchissement / petits travaux (+3 points)
    r"\btravaux\b|\bà rafra[iî]chir\b|\bremise aux normes\b|\bprévoir travaux\b": 3,
    
    # TYPOLOGIE : Immeubles & mono-propriété (+4 points)
    r"\bimmeuble( de rapport)?\b|\bmono-?propri[ée]t[ée]\b|\bensemble immobilier\b|\bplusieurs lots\b": 4,
    
    # POTENTIEL : Division / aménagement (+3 points)
    r"\bdivisible\b|\bdivision\b|\bpotentiel\b|\bd[ée]tachable\b": 3,
    
    # RATIONALISATION & SITUATION (+2 points)
    r"\bsuccession\b|\bvente rapide\b|\burgent\b|\bopportunit[ée]\b|\bd[ée]part\b": 2,
    r"\bgrange\b|\bgrenier\b|\bam[ée]nageable\b|\bcombles\b|\bremise\b|\bgarage\b|\bhangar\b": 2,
    r"\bnégociable\b|\bbaisse de prix\b|\bprix revu\b": 2,
    
    # RENDEMENT / LOCATIF (+1 point)
    r"\binvestisseur\b|\brendement\b|\blou[ée]\b|\blocataire\b": 1,
}

MOTS_REDHIBITOIRES = re.compile(
    r"\b(viager|vendu|vendue|sous compromis|compromis sign[ée]|sous offre|offre accept[ée]e)\b", re.I
)

# Filtres d'exclusion des terrains nus
RE_TERRAIN_SEUL = re.compile(
    r"\b(terrain|terrains|parcelle|parcelles|terrain à bâtir|terrain constructible)\b", re.I
)
RE_BATI = re.compile(
    r"\b(maison|maisons|appartement|appartements|immeuble|immeubles|bâtiment|batiment|grange|remise|local|locaux|studio|villa|duplex|loft|garage|hangar)\b", re.I
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
    motif = re.compile(r"^\s*(tout accepter|accepter|j.accepte|ok|continuer sans accepter)\s*$", re.I)
    try:
        btn = page.locator("button, a, input[type=button]").filter(has_text=motif).first
        if btn.count() and btn.is_visible(timeout=1000):
            btn.click(timeout=2000)
            page.wait_for_timeout(500)
    except Exception:
        pass


JS_NB_PAR_PAGE = """() => {
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
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        slug = re.sub(r"\W+", "_", nom.lower()).strip("_")
        (DEBUG_DIR / f"{slug}.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(DEBUG_DIR / f"{slug}.png"), full_page=True)
        log.warning("   📸 debug sauvegardé dans %s/%s.*", DEBUG_DIR, slug)
    except Exception as e:
        log.warning("   debug impossible : %s", e)


def charger_page(page, url: str, tentatives: int = 3) -> bool:
    for i in range(1, tentatives + 1):
        try:
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PWTimeout:
                pass
            return True
        except Exception as e:
            log.warning("Tentative %d/%d échouée pour %s : %s", i, tentatives, url, e)
            pause(2, 4)
    return False


def scroller_jusqu_au_bout(page, max_tours: int = 8) -> None:
    derniere = 0
    for _ in range(max_tours):
        hauteur = page.evaluate("document.body.scrollHeight")
        if hauteur == derniere:
            break
        derniere = hauteur
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(900)


JS_CARTES_PRIX = """() => {
  const out = new Set();
  document.querySelectorAll('a[href]').forEach(a => {
    let el = a;
    for (let i = 0; i < 5 && el; i++, el = el.parentElement) {
      const t = el.innerText || '';
      if (t.length > 1500) break;
      if (/\\d[\\d\\s.\\u00a0\\u202f]*(€|euros?)/i.test(t)) { out.add(a.href); break; }
    }
  });
  return [...out];
}"""


def _domaine(url: str) -> str:
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
        champ.press_sequentially(ville, delay=120)
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
        log.info("   🔎 URL filtrée : %s", page.url)
        return True
    except Exception as e:
        log.warning("   filtre « %s » en échec : %s", ville, e)
        return False


PREPARATIONS = {"filtre_ville": filtrer_par_ville}


def get_liens_agences_locales(context) -> dict[str, str]:
    resultats
