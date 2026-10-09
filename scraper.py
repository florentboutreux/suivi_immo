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
EXCLURE_TERRAINS_SEULS = True  # Écarte automatiquement les annonces qui concernent uniquement des terrains nus
MAX_PAGES_PAR_AGENCE = 5
MAX_FICHES_PAR_RUN = 200
VERSION_PARSEUR = 9
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
    {
        "nom": "iad",
        "url": "https://www.iadfrance.fr/annonces/millau-12100/vente",
        "pattern_fiche": r"/annonce/[^/]*millau[^/]*/r\d+",
        "mode": "pattern",
        "page_param": "page",
    },
    {
        "nom": "Expertimo",
        # Page directe Millau (435 = code Millau plateforme La Boite Immo)
        "url": "https://www.reseau-expertimo.fr/vente/435-millau/1",
        "pattern_fiche": r"/vente/\d+-millau/(?:[^/]+/)+\d{4,}-",
        "mode": "auto",
    },
    {
        "nom": "AP Immobilier",
        "url": "https://www.apimmobilier.fr/recherche/",
        "mode": "prix",
    },
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

# Filtre pour écarter les annonces qui concernent UNIQUEMENT un terrain nu (sans bâti)
RE_TERRAIN_PUR = re.compile(
    r"^s*(?:ventes+)?(?:terrain|parcelle)|terrains+(?:às+bâtir|constructible|agricole|des+loisir|nu|viabilisé)|parcelles+nue|/terrain[/-]",
    re.I
)
# Présence d'un bâtiment (qui justifie de garder le bien même s'il a du terrain)
RE_BATI_PRESENT = re.compile(
    r"\b(?:maison|immeuble|bâtisse|grange|hangar|atelier|remise|villa|appartement|corps de ferme|propriété|ruine|mazet|garage|local|plateau|chalet)\b",
    re.I
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

    # ÉCARTER LES ANNONCES DE TERRAINS NUS (SANS BÂTI EXISTANT)
    if EXCLURE_TERRAINS_SEULS:
        texte_verif = f"{titre} {h1} {urlparse(url).path}"
        if RE_TERRAIN_PUR.search(texte_verif) and not RE_BATI_PRESENT.search(texte_verif):
            log.info("   ✗ écartée (terrain seul sans bâti) : %s", url)
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

    # Double vérification : si 0 surface habitable et mention terrain prédominante
    if EXCLURE_TERRAINS_SEULS and surface is None:
        if re.search(r"\bterrain\b|\bparcelle\b", f"{titre} {h1}", re.I) and not RE_BATI_PRESENT.search(f"{titre} {h1}"):
            log.info("   ✗ écartée (terrain sans surface habitable) : %s", url)
            return None
    dpe = extraire_dpe(base, page.content())

    # HISTORIQUE DES PRIX & DÉTECTION SÉCURISÉE DES BAISSES (ANTI-FAUX POSITIFS)
    now_str = datetime.now().strftime("%d/%m/%Y")
    historique = []
    prix_initial = prix
    baisse_prix_pct = 0.0
    a_baisse = False

    if url in etat_existant:
        anc = etat_existant[url]
        # RECALIBRATION PROPRE LORS DU CHANGEMENT DE VERSION (évite de comparer avec l'ancien parseur buggé)
        if anc.get("v") != VERSION_PARSEUR:
            prix_initial = prix
            historique = [{"date": now_str, "prix": prix}]
            a_baisse = False
            baisse_prix_pct = 0.0
        else:
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
# EXPORT HTML MULTI-ONGLETS & SIMULATEUR EMBARQUÉ (GITHUB PAGES)
# --------------------------------------------------------------------------- #
TEMPLATE_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MDB Scanner Pro – Millau (12100)</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  :root { color-scheme: dark; }
</style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans pb-16">
  <!-- HEADER -->
  <header class="bg-slate-900 border-b border-slate-800 sticky top-0 z-30 shadow-lg">
    <div class="max-w-7xl mx-auto px-4 py-3 flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center gap-3">
        <div class="h-10 w-10 rounded-xl bg-amber-500 flex items-center justify-center text-slate-950 font-black text-lg shadow-md shadow-amber-500/20">
          MDB
        </div>
        <div>
          <div class="text-base font-bold text-white flex items-center gap-2">
            MDB Scanner Pro <span class="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/30">Millau 12100</span>
          </div>
          <div class="text-xs text-slate-400">Actualisé le : __DATE__</div>
        </div>
      </div>

      <nav class="flex space-x-1 sm:space-x-2">
        <button onclick="setTab('deals')" id="btn-tab-deals" class="px-3.5 py-1.5 rounded-lg text-xs font-bold transition bg-amber-500 text-slate-950 cursor-pointer">
          🏢 Annonces & Deals
        </button>
        <button onclick="setTab('simu')" id="btn-tab-simu" class="px-3.5 py-1.5 rounded-lg text-xs font-semibold transition text-slate-300 hover:bg-slate-800 cursor-pointer">
          🧮 Simulateur MDB
        </button>
        <button onclick="setTab('audit')" id="btn-tab-audit" class="px-3.5 py-1.5 rounded-lg text-xs font-semibold transition text-slate-300 hover:bg-slate-800 cursor-pointer">
          🎯 Stratégie MDB
        </button>
      </nav>
    </div>
  </header>

  <main class="max-w-7xl mx-auto px-4 py-6">
    <!-- ONGLET 1 : ANNONCES & DEALS -->
    <div id="tab-deals" class="space-y-6">
      <!-- KPIS -->
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-3" id="kpi-cards"></div>

      <!-- FILTRES -->
      <div class="bg-slate-900 p-4 rounded-2xl border border-slate-800 flex flex-wrap gap-3 items-center justify-between">
        <input type="text" id="q" oninput="renderDeals()" placeholder="Rechercher par rue, mot-clé (ex: 'rue droite', 'immeuble')..." class="px-3 py-2 bg-slate-950 border border-slate-700 rounded-xl text-xs text-white placeholder-slate-400 flex-1 min-w-[200px] focus:outline-none focus:border-amber-500" />
        <select id="f-dpe" onchange="renderDeals()" class="px-3 py-2 bg-slate-950 border border-slate-700 rounded-xl text-xs text-slate-200 focus:outline-none focus:border-amber-500">
          <option value="all">Tous DPE</option>
          <option value="passoires">🔥 Passoires F & G</option>
          <option value="G">DPE G uniquement</option>
          <option value="F">DPE F uniquement</option>
        </select>
        <label class="flex items-center gap-1.5 text-xs text-slate-300 cursor-pointer select-none">
          <input type="checkbox" id="f-drops" onchange="renderDeals()" class="rounded bg-slate-800 border-slate-700 text-amber-500" />
          <span>Baisses de prix uniquement</span>
        </label>
      </div>

      <!-- LISTE DES DEALS -->
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4" id="deals-grid"></div>
    </div>

    <!-- ONGLET 2 : SIMULATEUR MDB -->
    <div id="tab-simu" class="hidden space-y-6">
      <div class="bg-slate-900 p-6 rounded-2xl border border-slate-800">
        <h2 class="text-xl font-bold text-white mb-1">Simulateur Financier d'Opération & Découpe MDB</h2>
        <p class="text-xs text-slate-400 mb-6">Achat avec frais de notaire réduits MDB (0.715%), travaux et calcul de la marge de découpe.</p>

        <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">
          <div class="lg:col-span-7 space-y-4">
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
              <h3 class="text-xs font-bold text-amber-400 uppercase tracking-wider">1. Acquisition</h3>
              <div class="grid grid-cols-2 gap-3 text-xs">
                <div>
                  <label class="block text-slate-400 mb-1">Prix d'achat net vendeur (€)</label>
                  <input type="number" id="sim-prix" value="140000" oninput="calculerSimu()" class="w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded-lg text-white font-mono" />
                </div>
                <div>
                  <label class="block text-slate-400 mb-1">Frais de notaire</label>
                  <select id="sim-notaire" onchange="calculerSimu()" class="w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded-lg text-white font-mono">
                    <option value="0.00715">Marchand de Biens (0,715%)</option>
                    <option value="0.078">Particulier (7,8%)</option>
                  </select>
                </div>
              </div>
            </div>

            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
              <h3 class="text-xs font-bold text-amber-400 uppercase tracking-wider">2. Travaux & Portage</h3>
              <div class="grid grid-cols-2 gap-3 text-xs">
                <div>
                  <label class="block text-slate-400 mb-1">Enveloppe Travaux totale (€)</label>
                  <input type="number" id="sim-travaux" value="95000" oninput="calculerSimu()" class="w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded-lg text-amber-300 font-mono" />
                </div>
                <div>
                  <label class="block text-slate-400 mb-1">Frais financiers & portage (€)</label>
                  <input type="number" id="sim-portage" value="5500" oninput="calculerSimu()" class="w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded-lg text-slate-300 font-mono" />
                </div>
              </div>
            </div>

            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
              <h3 class="text-xs font-bold text-amber-400 uppercase tracking-wider">3. Revente (Chiffre d'Affaires total des lots)</h3>
              <div>
                <label class="block text-slate-400 text-xs mb-1">Revente totale estimée de tous les lots (€)</label>
                <input type="number" id="sim-revente" value="310000" oninput="calculerSimu()" class="w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded-lg text-emerald-400 font-mono font-bold" />
              </div>
            </div>
          </div>

          <!-- RESULTATS -->
          <div class="lg:col-span-5 bg-slate-950 p-5 rounded-2xl border border-slate-800 space-y-4">
            <h3 class="text-sm font-bold text-white uppercase tracking-wider">Résultats Financiers</h3>

            <div class="bg-emerald-950/40 border border-emerald-500/50 p-4 rounded-xl text-center">
              <span class="text-xs text-emerald-300 block">MARGE BRUTE OPÉRATION</span>
              <span class="text-2xl font-black text-emerald-400 font-mono" id="res-marge-brute">+68 500 €</span>
              <span class="text-xs text-emerald-300/80 block mt-1" id="res-marge-pct">22.1% du CA</span>
            </div>

            <div class="text-xs space-y-2 text-slate-400 font-mono border-t border-slate-800 pt-3">
              <div class="flex justify-between"><span>Prix d'achat :</span><span id="res-achat" class="text-white">140 000 €</span></div>
              <div class="flex justify-between"><span>Frais notaire :</span><span id="res-notaire" class="text-white">1 001 €</span></div>
              <div class="flex justify-between"><span>Travaux :</span><span id="res-travaux" class="text-white">95 000 €</span></div>
              <div class="flex justify-between"><span>Frais portage :</span><span id="res-portage" class="text-white">5 500 €</span></div>
              <div class="flex justify-between font-bold text-white pt-2 border-t border-slate-800"><span>Coût de revient total :</span><span id="res-revient">241 501 €</span></div>
              <div class="flex justify-between font-bold text-emerald-400"><span>Revente totale :</span><span id="res-revente">310 000 €</span></div>
            </div>

            <div class="bg-slate-900 p-3 rounded-xl border border-slate-800 text-[11px] text-slate-400">
              💡 <strong>Avantage MDB 0,715% :</strong> vous économisez environ <span id="res-gain-notaire" class="text-emerald-400 font-bold">+9 900 €</span> par rapport à un particulier acheteur.
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ONGLET 3 : STRATEGIE MDB -->
    <div id="tab-audit" class="hidden space-y-6">
      <div class="bg-slate-900 p-6 rounded-2xl border border-slate-800 space-y-4">
        <h2 class="text-xl font-bold text-white">Les 4 Piliers Rentables du Marchand de Biens à Millau</h2>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs text-slate-300">
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
            <strong class="text-amber-400 text-sm block mb-1">1. Monopropriétés Centre Ancien (Rue Droite, Halles)</strong>
            <p class="text-slate-400 leading-relaxed">Acheter des immeubles R+3 complets à 550 - 800 €/m² et découper en plusieurs lots (T2/T3). Aucun syndic de copro bloquant.</p>
          </div>
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
            <strong class="text-purple-400 text-sm block mb-1">2. Passoires thermiques G et F (Loi Climat)</strong>
            <p class="text-slate-400 leading-relaxed">Les bailleurs ne peuvent plus louer sans de lourds travaux. Négociation agressive (-20% à -30%) en mettant en avant le devis de rénovation globale.</p>
          </div>
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
            <strong class="text-emerald-400 text-sm block mb-1">3. Découpe de parcelles & Granges (Faubourgs)</strong>
            <p class="text-slate-400 leading-relaxed">Détachement d'un terrain constructible viabilisé pour amortir immédiatement l'acquisition, et rénovation du bâtiment restant.</p>
          </div>
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
            <strong class="text-sky-400 text-sm block mb-1">4. Prix de Revente Cible Millau</strong>
            <p class="text-slate-400 leading-relaxed">À Millau, les T2 et T3 rénovés avec DPE C/D se revendent entre 1 650 € et 2 100 €/m².</p>
          </div>
        </div>
      </div>
    </div>
  </main>

  <script>
    const DATA = __DATA__;

    function setTab(name) {
      ['deals', 'simu', 'audit'].forEach(t => {
        document.getElementById('tab-' + t).classList.toggle('hidden', t !== name);
        const btn = document.getElementById('btn-tab-' + t);
        if (t === name) {
          btn.className = 'px-3.5 py-1.5 rounded-lg text-xs font-bold transition bg-amber-500 text-slate-950 cursor-pointer';
        } else {
          btn.className = 'px-3.5 py-1.5 rounded-lg text-xs font-semibold transition text-slate-300 hover:bg-slate-800 cursor-pointer';
        }
      });
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    function simulerDeal(prix, surface) {
      document.getElementById('sim-prix').value = prix || 120000;
      const m2 = surface || 100;
      document.getElementById('sim-travaux').value = Math.round(m2 * 750);
      document.getElementById('sim-revente').value = Math.round(m2 * 1850);
      calculerSimu();
      setTab('simu');
    }

    function calculerSimu() {
      const prix = parseFloat(document.getElementById('sim-prix').value) || 0;
      const tauxNotaire = parseFloat(document.getElementById('sim-notaire').value) || 0.00715;
      const notaire = Math.round(prix * tauxNotaire);
      const travaux = parseFloat(document.getElementById('sim-travaux').value) || 0;
      const portage = parseFloat(document.getElementById('sim-portage').value) || 0;
      const revente = parseFloat(document.getElementById('sim-revente').value) || 0;

      const revient = prix + notaire + travaux + portage;
      const marge = revente - revient;
      const pct = revente > 0 ? ((marge / revente) * 100).toFixed(1) : 0;
      const gainNotaire = Math.round(prix * (0.078 - 0.00715));

      document.getElementById('res-achat').textContent = prix.toLocaleString('fr-FR') + ' €';
      document.getElementById('res-notaire').textContent = notaire.toLocaleString('fr-FR') + ' €';
      document.getElementById('res-travaux').textContent = travaux.toLocaleString('fr-FR') + ' €';
      document.getElementById('res-portage').textContent = portage.toLocaleString('fr-FR') + ' €';
      document.getElementById('res-revient').textContent = revient.toLocaleString('fr-FR') + ' €';
      document.getElementById('res-revente').textContent = revente.toLocaleString('fr-FR') + ' €';
      document.getElementById('res-marge-brute').textContent = (marge >= 0 ? '+' : '') + marge.toLocaleString('fr-FR') + ' €';
      document.getElementById('res-marge-pct').textContent = pct + '% du CA';
      document.getElementById('res-gain-notaire').textContent = '+' + gainNotaire.toLocaleString('fr-FR') + ' €';
    }

    function renderDeals() {
      const q = (document.getElementById('q').value || '').toLowerCase();
      const fdpe = document.getElementById('f-dpe').value;
      const fdrops = document.getElementById('f-drops').checked;

      const filtered = DATA.filter(a => {
        const matchesQ = !q || (a.titre + ' ' + (a.mots_detectes || '') + ' ' + (a.agence || '')).toLowerCase().includes(q);
        const matchesDpe = fdpe === 'all' || (fdpe === 'passoires' && (a.dpe === 'F' || a.dpe === 'G')) || a.dpe === fdpe;
        const matchesDrops = !fdrops || (a.baisse_prix_pct > 0);
        return matchesQ && matchesDpe && matchesDrops;
      });

      const total = DATA.length;
      const drops = DATA.filter(a => a.baisse_prix_pct > 0).length;
      const passoires = DATA.filter(a => a.dpe === 'F' || a.dpe === 'G').length;
      const news = DATA.filter(a => a.nouvelle).length;

      document.getElementById('kpi-cards').innerHTML =
        '<div class="bg-slate-900 border border-slate-800 p-3 rounded-xl"><b class="text-xl font-bold text-white block">' + total + '</b><span class="text-[10px] uppercase text-slate-400">Biens suivis</span></div>' +
        '<div class="bg-slate-900 border border-slate-800 p-3 rounded-xl"><b class="text-xl font-bold text-amber-400 block">' + news + '</b><span class="text-[10px] uppercase text-slate-400">Nouveautés</span></div>' +
        '<div class="bg-slate-900 border border-slate-800 p-3 rounded-xl"><b class="text-xl font-bold text-rose-400 block">' + drops + '</b><span class="text-[10px] uppercase text-slate-400">Baisses de prix</span></div>' +
        '<div class="bg-slate-900 border border-slate-800 p-3 rounded-xl"><b class="text-xl font-bold text-purple-400 block">' + passoires + '</b><span class="text-[10px] uppercase text-slate-400">Passoires F/G</span></div>';

      const grid = document.getElementById('deals-grid');
      grid.innerHTML = '';
      if (!filtered.length) {
        grid.innerHTML = '<div class="col-span-2 text-center py-12 text-slate-500">Aucun bien ne correspond aux filtres.</div>';
        return;
      }

      filtered.forEach(a => {
        const dpeClass = a.dpe === 'G' ? 'bg-purple-900 text-purple-100 ring-1 ring-purple-400' :
                         a.dpe === 'F' ? 'bg-rose-900 text-rose-100' :
                         a.dpe === 'E' ? 'bg-amber-900 text-amber-100' : 'bg-slate-800 text-slate-300';

        const card = document.createElement('div');
        card.className = 'bg-slate-900 border border-slate-800 p-5 rounded-2xl space-y-3 flex flex-col justify-between';
        card.innerHTML = 
          '<div>' +
            '<div class="flex items-center justify-between gap-2 mb-2">' +
              '<div class="flex items-center gap-1.5 flex-wrap">' +
                '<span class="px-2 py-0.5 rounded text-[11px] font-bold ' + dpeClass + '">DPE ' + (a.dpe || '?') + '</span>' +
                '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-amber-500/20 text-amber-300">Score ' + (a.score || 0) + '/10</span>' +
                (a.baisse_prix_pct > 0 ? '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-rose-500/20 text-rose-300">-' + a.baisse_prix_pct + '%</span>' : '') +
              '</div>' +
              '<span class="text-[11px] text-slate-500">' + (a.agence || '') + '</span>' +
            '</div>' +
            '<h3 class="text-base font-bold text-white leading-snug"><a href="' + a.url + '" target="_blank" class="hover:text-amber-400">' + (a.titre || a.url) + '</a></h3>' +
            '<div class="grid grid-cols-3 gap-2 bg-slate-950 p-2.5 rounded-xl border border-slate-800/80 my-3 text-center">' +
              '<div><span class="text-[9px] uppercase text-slate-500 block">Prix</span><b class="text-sm font-bold text-white">' + (a.prix ? a.prix.toLocaleString('fr-FR') + ' €' : '–') + '</b></div>' +
              '<div><span class="text-[9px] uppercase text-slate-500 block">Surface</span><b class="text-sm font-bold text-slate-300">' + (a.surface ? a.surface + ' m²' : '–') + '</b></div>' +
              '<div><span class="text-[9px] uppercase text-slate-500 block">Prix/m²</span><b class="text-sm font-bold text-slate-300">' + (a.prix_m2 ? a.prix_m2 + ' €' : '–') + '</b></div>' +
            '</div>' +
            '<div class="text-xs text-slate-400">' + (a.mots_detectes || '') + '</div>' +
          '</div>' +
          '<div class="pt-3 border-t border-slate-800 flex items-center justify-between gap-2">' +
            '<a href="' + a.url + '" target="_blank" class="text-xs text-slate-400 hover:text-white underline">Voir l\'annonce</a>' +
            '<button onclick="simulerDeal(' + (a.prix || 0) + ',' + (a.surface || 0) + ')" class="px-3 py-1.5 bg-amber-500 hover:bg-amber-400 text-slate-950 text-xs font-bold rounded-lg cursor-pointer">Simuler ce bien</button>' +
          '</div>';
        grid.appendChild(card);
      });
    }

    renderDeals();
    calculerSimu();
  </script>
</body>
</html>"""

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

    # Export des annonces valides (avec filtre anti-terrains nus renforcé)
    annonces = []
    for d in etat.values():
        if d.get("ignoree"):
            continue
        titre_l = (d.get("titre") or "").lower()
        surface_val = d.get("surface")

        # Écarter formellement tout terrain nu restant
        if EXCLURE_TERRAINS_SEULS:
            est_terrain_nu = (
                surface_val is None and any(w in titre_l for w in ["terrain", "parcelle"]) and
                not any(w in titre_l for w in ["maison", "immeuble", "grange", "bâtisse", "remise", "atelier", "villa", "appartement", "corps de ferme", "ruine", "mazet", "plateau"])
            )
            if est_terrain_nu:
                continue

        annonces.append(Annonce(**d))

    if PRIX_MAX:
        annonces = [a for a in annonces if a.prix is None or a.prix <= PRIX_MAX]
    annonces.sort(key=lambda a: (a.score or 0), reverse=True)

    exporter_csv(annonces)
    exporter_html(annonces)

    interessantes = [a for a in annonces if a.score >= SCORE_MINIMUM]
    log.info("🎯 Scan terminé avec succès ! %d annonces publiées (%d à fort potentiel MDB).", len(annonces), len(interessantes))
    print(f"\n✅ Terminé : {len(annonces)} annonces publiées dans docs/index.html")

if __name__ == "__main__":
    main()
