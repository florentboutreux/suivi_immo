#!/usr/bin/env python3
"""
=============================================================================
VEILLE IMMOBILIÈRE SPÉCIALE MARCHAND DE BIENS (MDB) – MILLAU (12100) v2.0
=============================================================================
Pipeline complet :
  1. Collecte des liens sur les agences de Millau (Roques, SGA, JMB, Notaires, Mesnard, iad, Expertimo, AP)
  2. Extraction fiche : Prix, Surface, Terrain, DPE (A à G), Titre, Mots-clés
  3. Suivi d'historique : Détection des baisses de prix & vendeur sous contrainte
  4. Scoring MDB : Division / Découpe, Passoires thermiques F & G (Loi Climat), Décote
  5. Alertes Push : Telegram ou Discord en direct sur smartphone
  6. Export : CSV + JSON d'état + Rapport HTML dans docs/index.html (GitHub Pages)
"""

import csv
import json
import logging
import os
import random
import re
import sys
import unicodedata
import urllib.request
from collections import deque
from dataclasses import dataclass, asdict, field
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
DOSSIER_DOCS = Path("docs")
FICHIER_HTML = DOSSIER_DOCS / "index.html"
DEBUG_DIR = Path("debug")

VILLES_AUTORISEES = ("millau",)
LOCALISATION_STRICTE = True

COMMUNES_VOISINES = [
    "verrieres", "le rozier", "rozier", "la cavalerie", "cavalerie", "nant", "saint georges de luzencon",
    "creissels", "compeyre", "aguessac", "paulhe", "compregnac", "peyreleau", "veyreau", "mostuejouls",
    "montjaux", "saint rome de cernon", "saint beauzely", "la couvertoirade", "severac", "saint affrique",
    "lapanouse", "viala du pas de jaux", "viala du tarn", "tournemire", "roquefort", "la roque sainte marguerite",
    "candas", "massegros", "meyrueis", "riviere sur tarn", "boyne", "la cresse", "saint leons", "salles curan",
    "lanuejols", "campestre", "le caylar", "saint jean du bruel", "saint laurent de levezou", "la bastide pradines",
    "saint paul des fonts", "saint martin de lenne", "sainte eulalie de cernon", "castelnau pegayrols",
    "saint germain", "montpellier", "rodez", "lodeve", "saint jean et saint paul", "balsac", "lavernhe",
    "saint georges", "saint sernin", "belmont sur rance", "sauclieres", "le clapier", "les vignes", "florac",
]

PRIX_MAX = 290_000          # Budget acquisition cible MDB
SCORE_MINIMUM = 2           # Seuil pour retenir le bien
SCORE_MINIMUM_ALERTE = 4    # Seuil pour déclencher une notification Telegram/Discord immédiate
MAX_PAGES_PAR_AGENCE = 5
MAX_FICHES_PAR_RUN = 200
VERSION_PARSEUR = 8
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ALERTES SMARTPHONE (laisser vide si non utilisé ou définir dans GitHub Secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

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
        "page_template": "https://www.roques-immobilier.com/vente/435-millau/{n}",
        "max_pages": 10,
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
    {
        # iad : mandataires indépendants. Page « Millau (12100) » : 45 biens, 30 par page (?page=2).
        # Fiches : /annonce/<type>-vente-<n>-pieces-millau-<m2>/r<id>. On ne garde que les slugs « millau »
        # (le 12100 inclut aussi Saint-Georges-de-Luzençon, La Roque-Sainte-Marguerite…).
        "nom": "iad",
        "url": "https://www.iadfrance.fr/annonces/millau-12100/vente",
        "pattern_fiche": r"/annonce/[^/]*millau[^/]*/r\d+",
        "mode": "pattern",
        "page_param": "page",
    },
    {
        # Expertimo (plateforme La Boîte Immo, comme Roques). « 29729 » est l'identifiant de Millau sur ce site :
        # page dédiée de 104 biens (9 par page, 12 pages au moment du test).
        "nom": "Expertimo",
        "url": "https://www.reseau-expertimo.fr/vente/29729-millau/1",
        "pattern_fiche": r"/vente/29729-millau/(?:[^/]+/)+\d{4,}-",
        "mode": "pattern",
        "page_template": "https://www.reseau-expertimo.fr/vente/29729-millau/{n}",
        "max_pages": 15,
    },
    {
        # /recherche/ n'existe pas : la liste est sur /biens (cartes générées en JavaScript, basée à Aguessac,
        # beaucoup de locations). Mode « prix » : un lien est retenu s'il est dans une carte affichant un prix.
        "nom": "AP Immobilier",
        "url": "https://www.apimmobilier.fr/biens",
        "mode": "prix",
    },
]
 
# Pages annexes à ignorer (testées sur le chemin, pas sur tout le lien)
]

EXCLUS = re.compile(
    r"(contact|mentions|legal|cgu|cgv|confidential|cookies|estimation|connexion|login|"
    r"inscri|actualit|blog|recrut|honoraires|facebook|instagram|linkedin|twitter|"
    r"youtube|mailto:|tel:|javascript:|\.pdf$|\.jpg$|\.png$)",
    re.I,
)

MOTS_CLES_MDB = {
    # 1. Division & Création de lots (Cœur de métier MDB)
    r"\bimmeuble( de rapport)?\b|\bmono-?propri[ée]t[ée]\b": 4,
    r"\bdivisible\b|\bdivision\b|\bd[ée]tachable\b|\bplusieurs lots\b": 4,
    r"\bplateau( brut)?\b|\bcombles am[ée]nageables?\b|\bgrenier\b": 3,
    r"\bgrange\b|\bhangar\b|\batelier\b|\bremise\b|\bgarage double\b": 2,
    r"\bterrain constructible\b|\bparcelle constructible\b": 3,

    # 2. Vendeurs sous pression
    r"\bsuccession\b|\bvendeur press[ée]\b|\bvente urgente\b|\bopportunit[ée]\b": 3,
    r"\bn[ée]gociable\b|\bbaisse de prix\b|\bprix r[ée]vis[ée]\b": 2,

    # 3. Travaux à fort levier de décote
    r"\b(à|a) r[ée]nover\b|\bgros travaux\b|\br[ée]habilitation compl[èe]te\b": 3,
    r"\btravaux\b|\bà rafra[iî]chir\b|\bà restaurer\b": 2,
}

MOTS_REDHIBITOIRES = re.compile(
    r"\b(viager|vendu|vendue|sous compromis|compromis sign[ée]|sous offre|offre accept[ée]e)\b", re.I
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scraper_mdb")

# --------------------------------------------------------------------------- #
# MODÈLE DE DONNÉES ENRICHI
# --------------------------------------------------------------------------- #
@dataclass
class Annonce:
    url: str
    agence: str
    titre: str = ""
    prix: int | None = None
    prix_initial: int | None = None
    baisse_prix_pct: float = 0.0
    historique_prix: list = field(default_factory=list)
    surface: float | None = None
    terrain: float | None = None
    prix_m2: int | None = None
    dpe: str = "INCONNU"
    score: int = 0
    mots_detectes: str = ""
    nouvelle: bool = False
    a_baisse: bool = False
    date_collecte: str = ""
    localisation: str = "Millau"
    v: int = VERSION_PARSEUR

# --------------------------------------------------------------------------- #
# NOTIFICATIONS TELEGRAM & DISCORD
# --------------------------------------------------------------------------- #
def envoyer_alerte_push(annonce: Annonce, motif: str = "PÉPITE DÉTECTÉE"):
    texte = (
        f"🚨 <b>MILLAU MDB : {motif} !</b>\n"
        f"🏢 <b>{annonce.titre}</b>\n"
        f"📍 {annonce.agence} | DPE : <b>{annonce.dpe}</b>\n"
        f"💰 Prix : <b>{annonce.prix:,} €</b> ({annonce.prix_m2 or '?'} €/m²)\n"
    )
    if annonce.baisse_prix_pct > 0:
        texte += f"📉 Baisse constatée : <b>-{annonce.baisse_prix_pct:.1f}%</b> (ancien : {annonce.prix_initial:,} €)\n"
    texte += f"🏷 Signaux : {annonce.mots_detectes}\n🔗 {annonce.url}"

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url_api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = json.dumps({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": texte,
                "parse_mode": "HTML",
                "disable_web_page_preview": False
            }).encode("utf-8")
            req = urllib.request.Request(url_api, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5):
                pass
            log.info("📲 Alerte Telegram envoyée !")
        except Exception as e:
            log.warning("Échec notification Telegram : %s", e)

    if DISCORD_WEBHOOK_URL:
        try:
            payload = json.dumps({"content": texte.replace("<b>", "**").replace("</b>", "**")}).encode("utf-8")
            req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5):
                pass
            log.info("📲 Alerte Discord envoyée !")
        except Exception as e:
            log.warning("Échec notification Discord : %s", e)

# --------------------------------------------------------------------------- #
# OUTILS EXTRACTION & NAVIGATION
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
    for _ in range(8):
        try:
            btn = page.locator("button, a").filter(has_text=motif).first
            if btn.count() and btn.is_visible(timeout=500):
                btn.click(timeout=2000)
                page.wait_for_timeout(1200)
            else:
                break
        except Exception:
            break

def charger_page(page, url: str, tentatives: int = 3) -> bool:
    for i in range(1, tentatives + 1):
        try:
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=6_000)
            except PWTimeout:
                pass
            return True
        except Exception as e:
            log.warning("Tentative %d/%d pour %s : %s", i, tentatives, url, e)
            pause(2, 4)
    return False

def scroller_jusqu_au_bout(page, max_tours: int = 6) -> None:
    derniere = 0
    for _ in range(max_tours):
        hauteur = page.evaluate("document.body.scrollHeight")
        if hauteur == derniere:
            break
        derniere = hauteur
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)

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

def aller_page_suivante(page, num_page: int = 1) -> bool:
    selecteurs = [
        "a[rel='next']", "a.next", "li.next a", ".pagination a.next",
        "a:has-text('Suivant')", "a:has-text('suivant')", "a:has-text('›')", "a:has-text('»')",
    ]
    for sel in selecteurs:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible(timeout=500):
                el.click(timeout=3000)
                page.wait_for_load_state("domcontentloaded", timeout=12_000)
                page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    try:
        suivant = re.compile(rf"^\s*{num_page + 1}\s*$")
        el = page.locator('[class*="pagin" i] a, nav[aria-label*="pag" i] a, ul.pagination a').filter(has_text=suivant).first
        if el.count() and el.is_visible(timeout=500):
            el.click(timeout=3000)
            page.wait_for_load_state("domcontentloaded", timeout=12_000)
            page.wait_for_timeout(1000)
            return True
    except Exception:
        pass
    return False

# --------------------------------------------------------------------------- #
# FILTRE VILLE JS (Notaires, Expertimo)
# --------------------------------------------------------------------------- #
SELECTEURS_CHAMP_VILLE = [
    'input[placeholder*="ville" i]', 'input[placeholder*="localisation" i]', 'input[placeholder*="où" i]',
    'input[placeholder*="code postal" i]', 'input[aria-label*="ville" i]', 'input[aria-label*="localisation" i]',
    'input[name*="ville" i]', 'input[id*="ville" i]', 'input[type="search"]',
]

def filtrer_par_ville(page, ville: str = "Millau") -> bool:
    try:
        champ = None
        for sel in SELECTEURS_CHAMP_VILLE:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=1000):
                champ = loc
                break
        if champ is None:
            return False
        champ.click(timeout=2000)
        champ.fill("")
        champ.press_sequentially(ville, delay=100)
        page.wait_for_timeout(1500)
        sugg = page.locator('[role="option"], li[class*="suggest" i], li').filter(has_text=re.compile(re.escape(ville), re.I)).first
        if sugg.count() and sugg.is_visible(timeout=800):
            sugg.click(timeout=2000)
        else:
            champ.press("Enter")
        page.wait_for_timeout(1500)
        return True
    except Exception:
        return False

PREPARATIONS = {"filtre_ville": filtrer_par_ville}

def get_liens_agences_locales(context) -> dict[str, str]:
    resultats: dict[str, str] = {}
    page = context.new_page()

    for agence in AGENCES:
        nom, url = agence["nom"], agence["url"]
        pattern = re.compile(agence.get("pattern_fiche", PATTERN_FICHE_DEFAUT), re.I)
        log.info("▶ %s : %s", nom, url)

        if not charger_page(page, url):
            continue
        mode = agence.get("mode", "auto")
        accepter_cookies(page)

        prep = agence.get("preparation")
        if prep and not PREPARATIONS[prep](page, agence.get("ville", "Millau")):
            continue

        for num_page in range(1, MAX_PAGES_PAR_AGENCE + 1):
            afficher_tout(page)
            scroller_jusqu_au_bout(page)
            nouveaux = extraire_liens_fiches(page, url, pattern, mode) - set(resultats)
            for lien in nouveaux:
                resultats[lien] = nom
            log.info("   page %d : +%d liens", num_page, len(nouveaux))
            if not nouveaux and num_page > 1:
                break
            page_param = agence.get("page_param")
            if page_param:
                sep = "&" if "?" in url else "?"
                if not charger_page(page, f"{url}{sep}{page_param}={num_page + 1}"):
                    break
            elif not aller_page_suivante(page, num_page):
                break
            pause()
        log.info("   → Total : %d fiches pour %s", len([k for k, v in resultats.items() if v == nom]), nom)
        pause()

    page.close()
    return resultats

# --------------------------------------------------------------------------- #
# EXTRACTION FICHE AVEC DPE & PRIX
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# EXTRACTION DU PRIX CANONIQUE & PROTECTION ANTI-FAUX-POSITIFS
# --------------------------------------------------------------------------- #
# JS pour extraire le prix OFFICIEL depuis les balises structurées (évite 100% des pièges de texte)
JS_PRIX_CANONIQUE = """() => {
  // 1. Balises méta canoniques (les plus fiables, insensibles au texte de l'annonce)
  const metaPrice = document.querySelector('meta[property="og:price:amount"], meta[property="product:price:amount"], meta[itemprop="price"], input[name*="prix" i]');
  if (metaPrice && metaPrice.content) {
    const val = parseInt(metaPrice.content.replace(/\D/g, ''), 10);
    if (val >= 15000 && val <= 5000000) return val;
  }

  // 2. JSON-LD structuré
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const data = JSON.parse(script.textContent);
      const items = Array.isArray(data) ? data : [data, data.offers, data['@graph']].flat().filter(Boolean);
      for (const item of items) {
        const p = item.price || (item.offers && item.offers.price);
        if (p) {
          const val = parseInt(String(p).replace(/\D/g, ''), 10);
          if (val >= 15000 && val <= 5000000) return val;
        }
      }
    } catch (e) {}
  }

  // 3. Sélecteurs CSS dédiés au bloc prix principal (en-tête de fiche)
  const sels = [
    '.fiche-prix', '.prix-bien', '.detail-prix', '.price-val', '.bien-prix',
    '.price', '[class*="price" i]', '[class*="prix" i]'
  ];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      const t = el.innerText || '';
      // Évite les blocs trop volumineux (qui contiennent les honoraires ou mensualités)
      if (t.length > 80) continue;
      const m = t.match(/(\d[\d\s.\u00a0\u202f]*)\s*(?:€|euros?)/i);
      if (m) {
        const val = parseInt(m[1].replace(/\D/g, ''), 10);
        if (val >= 15000 && val <= 5000000) return val;
      }
    }
  }
  return null;
}"""

_NUM = r"\d{1,3}(?:[ \u00a0\u202f.,]\d{3})+|\d{4,8}"
RE_PRIX = re.compile(
    rf"(?:€\s*({_NUM})(?![\d])|({_NUM})(?:[.,]\d{{1,2}})?\s*(?:€|euros?\b|eur\b))", re.I
)
CTX_POSITIF = re.compile(r"\bprix\b|\bvente\b|\bf\.a\.i\b|\bnet vendeur\b", re.I)
CTX_NEGATIF = re.compile(r"\bhonoraires?\b|\bfrais\b|\bcharges?\b|\btaxe\b|\bfonci[èe]re\b|\bloyer\b|\bpar mois\b|\bmensualit", re.I)
RE_SURFACE = re.compile(r"(\d{2,4}(?:[.,]\d{1,2})?)\s*m(?:²|2)\b", re.I)
RE_TERRAIN = re.compile(r"(?:terrain|parcelle|jardin)[^\d]{0,40}(\d{2,6}(?:[.,]\d{1,2})?)\s*m(?:²|2)", re.I)

RE_DPE = re.compile(
    r"(?:classe|[ée]tiquette|dpe|consommation)\s*(?:[ée]nerg[ée]tique)?\s*[:=\-\s]\s*([A-G]|vierge|non soumis)"
    r"|\b([A-G])\s*(?:\([0-9]{2,3}\s*kwh|kwh/m)",
    re.I
)

def extraire_dpe(texte: str, html: str = "") -> str:
    m = RE_DPE.search(texte)
    if m:
        lettre = (m.group(1) or m.group(2) or "").strip().upper()
        if lettre in ("A", "B", "C", "D", "E", "F", "G"):
            return lettre
        if "VIERGE" in lettre:
            return "VIERGE"
    m_html = re.search(r'class="[^"]*(?:dpe|energy)[^"]*([a-g])\b', html, re.I)
    if m_html:
        return m_html.group(1).upper()
    return "INCONNU"

def extraire_prix_fallback(texte: str) -> int | None:
    candidats = []
    for m in RE_PRIX.finditer(texte):
        val = int(re.sub(r"\D", "", m.group(1) or m.group(2)))
        if not 15_000 <= val <= 3_000_000:
            continue
        ctx = texte[max(0, m.start() - 50): m.end() + 30]
        # Pénalisation stricte des honoraires et taxes
        if CTX_NEGATIF.search(ctx):
            continue
        pts = 2 if CTX_POSITIF.search(ctx) else 0
        candidats.append((pts, -m.start(), val))
    if candidats:
        return max(candidats)[2]
    return None

class PageListe(Exception):
    def __init__(self, liens=()):
        super().__init__("page liste")
        self.liens = set(liens)

class FicheInjoignable(Exception):
    pass

JS_CONTENU = """() => {
  const cands = [...document.querySelectorAll('main, article, #content, .content')];
  const root = cands.sort((a, b) => b.textContent.length - a.textContent.length)[0] || document.body;
  const c = root.cloneNode(true);
  c.querySelectorAll('header, footer, nav, aside, script, style, [class*=footer], [class*=header], [class*=menu]').forEach(e => e.remove());
  return (c.textContent || '').replace(/\s+/g, ' ').trim();
}"""

def _norm(t: str) -> str:
    t = unicodedata.normalize("NFD", t.lower())
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()

RE_PROXIMITE = re.compile(r"\b(?:a|au|proche|quelques minutes|environs|vers)\s+de\s+millau", re.I)
RE_COMMUNES_VOISINES = re.compile(r"\b(" + "|".join(sorted(map(re.escape, COMMUNES_VOISINES), key=len, reverse=True)) + r")\b")

def evaluer_localisation(url: str, h1: str, contenu: str) -> str:
    path = urlparse(url).path
    if re.search(r"/\d+-millau/|-millau-", path, re.I):
        return "oui"
    t = RE_PROXIMITE.sub(" ", _norm(f"{h1} {contenu[:2500]}"))
    if "millau" in t and not RE_COMMUNES_VOISINES.search(t):
        return "oui"
    if RE_COMMUNES_VOISINES.search(t):
        return "non"
    return "oui" if "millau" in t else "incertain"

def analyser_fiche(page, url: str, agence: str, etat_existant: dict) -> Annonce | None:
    if not charger_page(page, url, tentatives=2):
        raise FicheInjoignable(url)

    try:
        page.wait_for_function("document.body && /€|euro/i.test(document.body.innerText)", timeout=4000)
    except PWTimeout:
        pass

    try:
        titre = h1 = page.locator("h1").first.inner_text(timeout=2000).strip()
    except Exception:
        titre, h1 = page.title(), ""

    texte = page.inner_text("body")
    contenu = page.evaluate(JS_CONTENU)

    m_exclu = MOTS_REDHIBITOIRES.search(f"{h1} {contenu[:1500]}")
    if m_exclu:
        return None

    statut = evaluer_localisation(url, h1, contenu)
    if statut == "non" or (statut == "incertain" and LOCALISATION_STRICTE):
        return None

    base = contenu if len(contenu) >= 200 else texte
    # Priorité 1 : Balise méta ou sélecteur de prix dédié (aucun risque de capturer des honoraires)
    prix = page.evaluate(JS_PRIX_CANONIQUE)
    # Priorité 2 : Repli avec exclusion stricte des mentions de charges/taxes
    if prix is None:
        prix = extraire_prix_fallback(base)
    if prix is None:
        return None

    surfaces = [float(s.replace(",", ".")) for s in RE_SURFACE.findall(base)]
    surface = next((s for s in surfaces if 15 <= s <= 1500), None)
    m_terrain = RE_TERRAIN.search(base)
    terrain = float(m_terrain.group(1).replace(",", ".")) if m_terrain else None
    dpe = extraire_dpe(base, page.content())

    # HISTORIQUE DES PRIX & DÉTECTION SÉCURISÉE DES BAISSES (ANTI-FAUX POSITIFS)
    now_str = datetime.now().strftime("%d/%m/%Y")
    historique = []
    prix_initial = prix
    baisse_prix_pct = 0.0
    a_baisse = False

    if url in etat_existant:
        anc = etat_existant[url]
        prix_initial = anc.get("prix_initial") or anc.get("prix") or prix
        historique = anc.get("historique_prix", [])
        ancien_prix = anc.get("prix")

        # VERROUILLAGE ANTI-FAUX POSITIFS :
        # Une baisse réelle doit être >= 3.0% ET >= 3 000 € d'écart (élimine les arrondis FAI / Net Vendeur)
        # Et elle ne doit PAS dépasser 35% (au-delà, c'est une erreur de capture de loyer ou charges)
        if ancien_prix and prix < ancien_prix:
            delta_euros = ancien_prix - prix
            delta_pct = ((ancien_prix - prix) / ancien_prix) * 100

            if delta_euros >= 3000 and 3.0 <= delta_pct <= 35.0:
                a_baisse = True
                baisse_prix_pct = round(((prix_initial - prix) / prix_initial) * 100, 1)
                log.info("📉 VRAIE BAISSE DE PRIX VALIDÉE (-%.1f%%) sur %s : %d € -> %d € (-%d €)", baisse_prix_pct, url, ancien_prix, prix, delta_euros)
            else:
                log.warning("⚠️ Écart de prix ignoré (faux positif évité) sur %s : %d € vs %d € (delta: %d € / %.1f%%)", url, ancien_prix, prix, delta_euros, delta_pct)

    if not historique or historique[-1].get("prix") != prix:
        historique.append({"date": now_str, "prix": prix})

    # SCORING MDB
    score, detectes = 0, []
    texte_bas = f"{titre} {base}".lower()

    for regex, points in MOTS_CLES_MDB.items():
        m = re.search(regex, texte_bas, re.I)
        if m:
            score += points
            detectes.append(m.group(0).strip())

    # Bonus DPE Passoire thermique (G = +4, F = +3)
    if dpe == "G":
        score += 4
        detectes.append("Passoire G (Audit & Travaux)")
    elif dpe == "F":
        score += 3
        detectes.append("Passoire F")

    # Bonus Baisse de prix
    if a_baisse or baisse_prix_pct >= 5:
        score += 3
        detectes.append(f"Baisse -{baisse_prix_pct:.0f}%")

    prix_m2 = int(prix / surface) if prix and surface else None
    if prix_m2 and prix_m2 < 1100:
        score += 3
        detectes.append(f"Prix canon {prix_m2} €/m²")

    return Annonce(
        url=url, agence=agence, titre=titre[:140], prix=prix,
        prix_initial=prix_initial, baisse_prix_pct=baisse_prix_pct,
        historique_prix=historique, a_baisse=a_baisse,
        surface=surface, terrain=terrain, prix_m2=prix_m2, dpe=dpe,
        score=score, mots_detectes=", ".join(dict.fromkeys(detectes)),
        date_collecte=datetime.now().strftime("%Y-%m-%d %H:%M"),
        localisation="Millau",
    )

# --------------------------------------------------------------------------- #
# EXPORT HTML & CSV
# --------------------------------------------------------------------------- #
TEMPLATE_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MDB Millau – Veille Marchand de Biens</title>
<style>
  :root { --bg:#0b0f17; --card:#131a26; --txt:#f1f5f9; --mut:#94a3b8; --bd:#1e293b; --acc:#f59e0b; --new:#ef4444; --ok:#10b981; }
  * { box-sizing:border-box; font-family:system-ui,-apple-system,sans-serif; }
  body { margin:0; background:var(--bg); color:var(--txt); padding:20px; font-size:14px; }
  header { max-width:1300px; margin:auto; display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--bd); padding-bottom:16px; flex-wrap:wrap; gap:10px; }
  h1 { margin:0; font-size:22px; color:var(--acc); }
  .kpis { display:flex; gap:12px; max-width:1300px; margin:16px auto; flex-wrap:wrap; }
  .kpi { background:var(--card); border:1px solid var(--bd); border-radius:10px; padding:12px 18px; }
  .kpi b { font-size:22px; display:block; color:#fff; }
  .kpi span { font-size:11px; color:var(--mut); text-transform:uppercase; }
  table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--bd); border-radius:10px; overflow:hidden; }
  th, td { padding:10px 12px; text-align:left; border-bottom:1px solid var(--bd); white-space:nowrap; }
  th { font-size:11px; text-transform:uppercase; color:var(--mut); }
  td.wrap { white-space:normal; min-width:280px; }
  a { color:var(--acc); text-decoration:none; font-weight:600; }
  .dpe { display:inline-block; padding:2px 8px; border-radius:4px; font-weight:800; font-size:11px; }
  .dpe-G { background:#581c87; color:#f3e8ff; border:1px solid #a855f7; }
  .dpe-F { background:#991b1b; color:#fee2e2; }
  .dpe-E { background:#c2410c; color:#ffedd5; }
  .drop { color:#f43f5e; font-weight:bold; }
  .badge { padding:2px 6px; border-radius:4px; font-size:10px; font-weight:bold; margin-left:6px; background:#ef4444; color:#fff; }
</style>
</head>
<body>
<header>
  <div><h1>Veille Marchand de Biens – Millau (12100)</h1><div style="color:var(--mut);font-size:12px">Actualisé le : __DATE__</div></div>
</header>
<div class="kpis" id="kpis"></div>
<div style="max-width:1300px;margin:auto;overflow-x:auto;">
  <table>
    <thead><tr><th>Score</th><th>DPE</th><th>Annonce</th><th>Agence</th><th>Prix</th><th>Baisse</th><th>Surface</th><th>€/m²</th><th>Signaux MDB</th></tr></thead>
    <tbody id="body"></tbody>
  </table>
</div>
<script>
const DATA = __DATA__;
const $ = id => document.getElementById(id);
const fmt = (v, suf="") => v == null ? "–" : Number(v).toLocaleString("fr-FR") + suf;

const total = DATA.length;
const drops = DATA.filter(a => a.baisse_prix_pct > 0).length;
const passoires = DATA.filter(a => a.dpe === 'F' || a.dpe === 'G').length;
const news = DATA.filter(a => a.nouvelle).length;

$("kpis").innerHTML = 
  '<div class="kpi"><b>' + total + '</b><span>Biens suivis</span></div>' +
  '<div class="kpi"><b>' + news + '</b><span>Nouvelles</span></div>' +
  '<div class="kpi"><b>' + drops + '</b><span>Baisses de prix</span></div>' +
  '<div class="kpi"><b>' + passoires + '</b><span>Passoires F/G</span></div>';

DATA.sort((a,b) => (b.score || 0) - (a.score || 0));
DATA.forEach(a => {
  const tr = document.createElement("tr");
  tr.innerHTML = 
    '<td><b>' + (a.score || 0) + '/10</b></td>' +
    '<td><span class="dpe dpe-' + (a.dpe || '') + '">' + (a.dpe || '–') + '</span></td>' +
    '<td class="wrap"><a href="' + a.url + '" target="_blank">' + (a.titre || a.url) + '</a>' + (a.nouvelle ? '<span class="badge">NOUVEAU</span>' : '') + '</td>' +
    '<td>' + (a.agence || '') + '</td>' +
    '<td><b>' + fmt(a.prix, " €") + '</b></td>' +
    '<td class="drop">' + (a.baisse_prix_pct > 0 ? '-' + a.baisse_prix_pct + '%' : '–') + '</td>' +
    '<td>' + fmt(a.surface, " m²") + '</td>' +
    '<td>' + fmt(a.prix_m2, " €") + '</td>' +
    '<td class="wrap" style="color:var(--mut);font-size:12px">' + (a.mots_detectes || '') + '</td>';
  $("body").appendChild(tr);
});
</script>
</body></html>"""

def exporter_html(annonces: list[Annonce]):
    DOSSIER_DOCS.mkdir(exist_ok=True)
    donnees = json.dumps([asdict(a) for a in annonces], ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE_HTML.replace("__DATA__", donnees).replace("__DATE__", datetime.now().strftime("%d/%m/%Y à %H:%M"))
    FICHIER_HTML.write_text(html, encoding="utf-8")
    (DOSSIER_DOCS / ".nojekyll").touch()
    log.info("📄 Rapport HTML généré : %s", FICHIER_HTML)

def exporter_csv(annonces: list[Annonce]):
    DATA_DIR.mkdir(exist_ok=True)
    if not annonces:
        return
    with FICHIER_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(annonces[0])), delimiter=";")
        w.writeheader()
        w.writerows(asdict(a) for a in annonces)

# --------------------------------------------------------------------------- #
# MAIN SCRAPER RUNNER
# --------------------------------------------------------------------------- #
def main():
    DATA_DIR.mkdir(exist_ok=True)
    etat = {}
    if FICHIER_ETAT.exists():
        try:
            etat = json.loads(FICHIER_ETAT.read_text(encoding="utf-8"))
        except Exception:
            etat = {}

    log.info("🚀 Démarrage du scraper MDB Millau (état actuel : %d annonces en mémoire)", len(etat))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="fr-FR")
            context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("image", "font", "media")
                else route.continue_(),
            )

            liens = get_liens_agences_locales(context)
            log.info("✨ %d liens de fiches collectés au total", len(liens))

            inconnues = [(u, a) for u, a in liens.items() if u not in etat]
            obsoletes = [(u, a) for u, a in liens.items() if u in etat and etat[u].get("v") != VERSION_PARSEUR]
            a_traiter = (inconnues + obsoletes)[:MAX_FICHES_PAR_RUN]
            log.info("🆕 %d fiches à analyser...", len(a_traiter))

            page = context.new_page()
            file = deque(a_traiter)
            traitees = set()

            while file:
                url, agence = file.popleft()
                traitees.add(url)
                deja_connue = url in etat

                try:
                    annonce = analyser_fiche(page, url, agence, etat)
                except Exception as e:
                    log.warning("Erreur analyse %s : %s", url, e)
                    continue

                if annonce:
                    annonce.nouvelle = not deja_connue
                    etat[url] = asdict(annonce)
                    # Alerte push si nouvelle pépite ou baisse
                    if (annonce.nouvelle and annonce.score >= SCORE_MINIMUM_ALERTE) or annonce.a_baisse:
                        motif = "BAISSE DE PRIX" if annonce.a_baisse else "NOUVELLE PÉPITE"
                        envoyer_alerte_push(annonce, motif)
                else:
                    etat[url] = {"url": url, "ignoree": True, "v": VERSION_PARSEUR}
                pause()

        finally:
            browser.close()

    # Sauvegarde de l'état
    FICHIER_ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=1), encoding="utf-8")

    annonces = [
        Annonce(**d) for d in etat.values()
        if not d.get("ignoree") and d.get("v") == VERSION_PARSEUR
    ]
    annonces = [a for a in annonces if PRIX_MAX is None or a.prix is None or a.prix <= PRIX_MAX]
    annonces.sort(key=lambda a: (a.score or 0), reverse=True)

    exporter_csv(annonces)
    exporter_html(annonces)

    interessantes = [a for a in annonces if a.score >= SCORE_MINIMUM]
    log.info("🎯 Scan terminé avec succès ! %d annonces pertinentes à Millau.", len(interessantes))
    print(f"\n✅ Terminé : {len(annonces)} annonces publiées dans docs/index.html")

if __name__ == "__main__":
    main()
