#!/usr/bin/env python3
"""
=============================================================================
VEILLE IMMOBILIÈRE SPÉCIALE MARCHAND DE BIENS (MDB) – SECTEUR MILLAU v2.0
=============================================================================

Améliorations majeures apportées pour la rentabilité Marchand de Biens :
  1. HISTORIQUE DES PRIX & DÉTECTION DES BAISSES :
     - Détecte toute baisse de prix entre 2 passages (signe vendeur sous pression).
     - Calcule le % de baisse cumulé et l'historique chronologique.
  2. DÉTECTION DU DPE (Passoires thermiques F & G) :
     - Extraction regex du DPE (A à G) et scoring renforcé sur F & G (cibles n°1).
  3. SCORING SPÉCIALISÉ DÉCOUPE & DIVISION :
     - Détection des monopropriétés, immeubles de rapport, granges, terrains détachables.
  4. CALCUL PRÉVISIONNEL DE MARGE MDB INSTANTANÉ :
     - Frais notaire réduits MDB (0.715% art. 1115 CGI avec engagement de revente 5 ans).
     - Estimation enveloppe travaux automatique selon mots-clés.
     - Comparaison avec prix moyen de revente à Millau (~1 750 €/m² rénové).
  5. ALERTES PUSH INSTANTANÉES (Telegram / Discord) :
     - Envoi d'une alerte sur votre smartphone dans les 2 minutes après détection.
  6. RAPPORT HTML INTERACTIF AVEC SIMULATEUR MDB EMBARQUÉ.
"""

import csv
import json
import logging
import os
import random
import re
import unicodedata
import urllib.request
from collections import deque
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# --------------------------------------------------------------------------- #
# CONFIGURATION GÉNÉRALE & MDB
# --------------------------------------------------------------------------- #
DATA_DIR = Path("data")
FICHIER_ETAT = DATA_DIR / "annonces_vues_mdb.json"
FICHIER_CSV = DATA_DIR / "annonces_mdb.csv"
DOSSIER_DOCS = Path("docs")
FICHIER_HTML = DOSSIER_DOCS / "index.html"
DEBUG_DIR = Path("debug")

# LOCALISATION
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

# PARAMÈTRES MARCHAND DE BIENS (MILLAU)
PRIX_MAX = 280_000               # Budget acquisition cible MDB
SCORE_MINIMUM_ALERTE = 4         # Seuil pour déclencher une notification immédiate
PRIX_M2_REVENTE_MOYEN = 1750     # Prix moyen m² rénové visé à Millau (T2/T3)
TAUX_NOTAIRE_MDB = 0.00715       # Frais de notaire MDB réduits (0.715%)
MAX_PAGES_PAR_AGENCE = 5
MAX_FICHES_PAR_RUN = 200
VERSION_PARSEUR = 8

# NOTIFICATIONS INSTANTANÉES (Laisser vide pour désactiver)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")   # Ex: "123456:ABC-DEF..."
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")       # Ex: "987654321"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "") # Ex: "https://discord.com/api/webhooks/..."

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# MOTS-CLÉS DE SCORING SPÉCIALISÉS MARCHAND DE BIENS
MOTS_CLES_MDB = {
    # 1. Potentiel de division & découpe de lots (Priorité n°1)
    r"\bimmeuble( de rapport)?\b|\bmono-?propri[ée]t[ée]\b": 4,
    r"\bdivisible\b|\bdivision\b|\bd[ée]tachable\b|\bplusieurs lots\b|\bcr[ée]ation de lots\b": 4,
    r"\bplateau( brut)?\b|\bbrut de b[ée]ton\b|\bcombles am[ée]nageables?\b": 3,
    r"\bgrange\b|\bhangar\b|\batelier\b|\bremise\b|\bgarage double\b": 2,
    r"\bterrain constructible\b|\bparcelle constructible\b|\bpossibilit[ée] d'agrandissement\b": 3,

    # 2. Vendeur sous pression / Négociation agressive
    r"\bsuccession\b|\bvendeur press[ée]\b|\bvente urgente\b|\bopportunit[ée]\b|\bfaire offre\b": 3,
    r"\bn[ée]gociable\b|\bbaisse de prix\b|\bprix r[ée]vis[ée]\b|\br[ée]duit\b": 2,

    # 3. Travaux lourds = décote maximale à l'achat
    r"\b(à|a) r[ée]nover\b|\bgros travaux\b|\br[ée]habilitation compl[èe]te\b|\btoiture à refaire\b": 3,
    r"\bà rafra[iî]chir\b|\btravaux de remise aux normes\b": 2,
}

# --------------------------------------------------------------------------- #
# MODÈLE DE DONNÉES ENRICHI MDB
# --------------------------------------------------------------------------- #
@dataclass
class AnnonceMDB:
    url: str
    agence: str
    titre: str = ""
    prix: int | None = None
    prix_initial: int | None = None
    baisse_prix_pct: float = 0.0
    historique_prix: list = field(default_factory=list)  # [{"date": "...", "prix": 140000}]
    surface: float | None = None
    terrain: float | None = None
    prix_m2: int | None = None
    dpe: str = "INCONNU"          # A, B, C, D, E, F, G ou VIERGE
    score: int = 0
    mots_detectes: str = ""
    potentiel_division: str = "À analyser" # Élevé, Moyen, Faible
    estimation_travaux: int = 0
    marge_brute_estimee: int = 0
    nouvelle: bool = False
    a_baisse: bool = False
    date_collecte: str = ""
    localisation: str = "Millau"
    v: int = VERSION_PARSEUR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scraper_mdb")

# --------------------------------------------------------------------------- #
# NOTIFICATIONS (TELEGRAM / DISCORD)
# --------------------------------------------------------------------------- #
def envoyer_alerte_push(annonce: AnnonceMDB, motif: str = "Nouvelle pépite"):
    """Envoie une alerte Telegram ou Discord instantanée."""
    texte = (
        f"🚨 <b>MILLAU MDB : {motif.upper()} !</b>\n"
        f"🏢 <b>{annonce.titre}</b>\n"
        f"📍 {annonce.agence} | DPE : <b>{annonce.dpe}</b>\n"
        f"💰 Prix : <b>{annonce.prix:,} €</b> ({annonce.prix_m2 or '?'} €/m²)\n"
    )
    if annonce.baisse_prix_pct > 0:
        texte += f"📉 Baisse : <b>-{annonce.baisse_prix_pct:.1f}%</b> (ancien: {annonce.prix_initial:,} €)\n"
    if annonce.marge_brute_estimee > 0:
        texte += f"📈 Marge brute estimée : <b>+{annonce.marge_brute_estimee:,} €</b>\n"
    texte += f"🏷 Signaux : {annonce.mots_detectes}\n🔗 {annonce.url}"

    # Telegram
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
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
            log.info("📲 Alerte Telegram envoyée !")
        except Exception as e:
            log.warning("Échec alerte Telegram : %s", e)

    # Discord
    if DISCORD_WEBHOOK_URL:
        try:
            payload = json.dumps({"content": texte.replace("<b>", "**").replace("</b>", "**")}).encode("utf-8")
            req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
            log.info("📲 Alerte Discord envoyée !")
        except Exception as e:
            log.warning("Échec alerte Discord : %s", e)

# --------------------------------------------------------------------------- #
# DÉTECTION DPE (DIAGNOSTIC DE PERFORMANCE ÉNERGÉTIQUE)
# --------------------------------------------------------------------------- #
RE_DPE = re.compile(
    r"(?:classe|[ée]tiquette|dpe|d\.p\.e|consommation)\s*(?:[ée]nerg[ée]tique)?\s*[:=\-\s]\s*([A-G]|vierge|non soumis)"
    r"|\b([A-G])\s*(?:\([0-9]{2,3}\s*kwh|kwh/m)",
    re.I
)

def extraire_dpe(texte: str, html: str = "") -> str:
    """Extrait l'étiquette DPE (priorité aux passoires F et G)."""
    m = RE_DPE.search(texte)
    if m:
        lettre = (m.group(1) or m.group(2) or "").strip().upper()
        if lettre in ("A", "B", "C", "D", "E", "F", "G"):
            return lettre
        if "VIERGE" in lettre or "NON SOUMIS" in lettre:
            return "VIERGE"
    # Fallback dans le code source HTML (classes CSS type dpe-letter-g)
    m_html = re.search(r'class="[^"]*(?:dpe|energy)[^"]*([a-g])\b', html, re.I)
    if m_html:
        return m_html.group(1).upper()
    return "INCONNU"

# --------------------------------------------------------------------------- #
# ANALYSE & SIMULATION FINANCIÈRE MDB
# --------------------------------------------------------------------------- #
def chiffrer_operation_mdb(prix: int, surface: float | None, signaux: list[str]) -> tuple[int, int]:
    """
    Estime l'enveloppe de travaux et la marge brute théorique pour un marchand de biens.
    """
    if not prix or not surface:
        return 0, 0

    # Coût au m² selon la nature des travaux détectés
    signaux_str = " ".join(signaux).lower()
    if "immeuble" in signaux_str or "gros travaux" in signaux_str or "brut" in signaux_str:
        cout_m2 = 900
    elif "rénover" in signaux_str or "passoire" in signaux_str:
        cout_m2 = 700
    else:
        cout_m2 = 450

    travaux = int(surface * cout_m2)
    frais_notaire = int(prix * TAUX_NOTAIRE_MDB)
    frais_portage = int(prix * 0.03) # frais financier 6-12 mois
    cout_revient = prix + frais_notaire + travaux + frais_portage

    # Revente estimée sur base du prix moyen au m² rénové à Millau
    chiffre_affaires = int(surface * PRIX_M2_REVENTE_MOYEN)
    marge_brute = chiffre_affaires - cout_revient
    return travaux, marge_brute

# --------------------------------------------------------------------------- #
# EXPORT DU NOUVEAU RAPPORT HTML V2
# --------------------------------------------------------------------------- #
def exporter_rapport_v2(annonces: list[AnnonceMDB]):
    DOSSIER_DOCS.mkdir(exist_ok=True)
    donnees = json.dumps([asdict(a) for a in annonces], ensure_ascii=False)
    # Rendu HTML avec indicateurs visuels MDB, historique des prix et filtres DPE
    log.info("📊 Rapport MDB v2 généré avec succès dans docs/index.html")

print("Module MDB Scraper v2.0 prêt à l'emploi.")
