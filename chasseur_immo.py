agences · PY
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
 
PRIX_MAX = 250_000          # budget maximum (None pour désactiver)
SCORE_MINIMUM = 2           # seuil pour être jugée "intéressante"
MAX_PAGES_PAR_AGENCE = 5    # pagination
MAX_FICHES_PAR_RUN = 150    # garde-fou
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
MOTS_REDHIBITOIRES = re.compile(r"\b(viager|vendu|sous compromis|sous offre|loué)\b", re.I)
 
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
    for texte in ("Tout accepter", "Accepter", "J'accepte", "OK", "Continuer sans accepter"):
        try:
            btn = page.get_by_role("button", name=re.compile(texte, re.I)).first
            if btn.is_visible(timeout=800):
                btn.click(timeout=1500)
                return
        except Exception:
            continue
 
 
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
 
 
def extraire_liens_fiches(page, base_url: str, pattern: re.Pattern) -> set[str]:
    # .href renvoie déjà l'URL ABSOLUE (corrige les liens relatifs mal résolus)
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    domaine = urlparse(base_url).netloc
    liens = set()
    for href in hrefs:
        href = normaliser_url(href)
        p = urlparse(href)
        if p.scheme not in ("http", "https") or p.netloc != domaine:
            continue
        if EXCLUS.search(href):
            continue
        # le motif ne s'applique qu'au chemin + query, pas au domaine
        if not pattern.search(p.path + ("?" + p.query if p.query else "")):
            continue
        if href == normaliser_url(base_url):
            continue
        liens.add(href)
    return liens
 
 
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
        accepter_cookies(page)
 
        total_agence = 0
        for num_page in range(1, MAX_PAGES_PAR_AGENCE + 1):
            scroller_jusqu_au_bout(page)
            nouveaux = extraire_liens_fiches(page, url, pattern) - set(resultats)
            for lien in nouveaux:
                resultats[lien] = nom
            total_agence += len(nouveaux)
            log.info("   page %d : +%d liens", num_page, len(nouveaux))
 
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
RE_PRIX = re.compile(r"(\d{1,3}(?:[\s\u00a0\u202f.]\d{3})+|\d{4,7})\s*(?:€|euros?|eur)\b", re.I)
RE_SURFACE = re.compile(r"(\d{2,4}(?:[.,]\d{1,2})?)\s*m(?:²|2)\b", re.I)
RE_TERRAIN = re.compile(r"(?:terrain|parcelle|jardin)[^\d]{0,40}(\d{2,6}(?:[.,]\d{1,2})?)\s*m(?:²|2)", re.I)
 
 
def _nombre(txt: str) -> float:
    return float(re.sub(r"[\s\u00a0\u202f]", "", txt).replace(".", "").replace(",", ".")) \
        if re.search(r"\d[\s\u00a0\u202f.]\d{3}", txt) else float(txt.replace(",", "."))
 
 
def analyser_fiche(page, url: str, agence: str) -> Annonce | None:
    if not charger_page(page, url, tentatives=2):
        return None
 
    try:
        titre = page.locator("h1").first.inner_text(timeout=3000).strip()
    except Exception:
        titre = page.title()
 
    texte = page.inner_text("body")
    if MOTS_REDHIBITOIRES.search(texte[:3000]):
        log.info("   ✗ écartée (vendu/viager/etc.) : %s", url)
        return None
 
    # Prix : on prend la première valeur plausible (> 10 k€)
    prix = None
    for m in RE_PRIX.finditer(texte):
        val = int(_nombre(m.group(1)))
        if 10_000 <= val <= 5_000_000:
            prix = val
            break
 
    surfaces = [float(s.replace(",", ".")) for s in RE_SURFACE.findall(texte)]
    surface = next((s for s in surfaces if 15 <= s <= 1500), None)
    m_terrain = RE_TERRAIN.search(texte)
    terrain = float(m_terrain.group(1).replace(",", ".")) if m_terrain else None
 
    # Scoring
    score, detectes = 0, []
    texte_bas = texte.lower()
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
<title>Veille immobilière – Millau</title>
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
    (!nouv || a.nouvelle) && (!q || (a.titre + " " + a.mots_detectes).toLowerCase().includes(q))
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
    cell(tr, a.agence);
    cell(tr, fmt(a.prix, " €")); cell(tr, fmt(a.surface, " m²")); cell(tr, fmt(a.terrain, " m²")); cell(tr, fmt(a.prix_m2, " €"));
    cell(tr, a.mots_detectes, "kw wrap-txt");
    body.appendChild(tr);
  });
}
["q","agence","smin","pmax","nouv"].forEach(id => $(id).addEventListener("input", render));
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
 
            # On n'analyse que les fiches inconnues (gain de temps énorme au fil des jours)
            a_traiter = [(u, a) for u, a in liens.items() if u not in etat][:MAX_FICHES_PAR_RUN]
            log.info("🆕 %d nouvelles fiches à analyser", len(a_traiter))
 
            page = context.new_page()
            for i, (url, agence) in enumerate(a_traiter, 1):
                log.info("[%d/%d] %s", i, len(a_traiter), url)
                annonce = analyser_fiche(page, url, agence)
                if annonce:
                    annonce.nouvelle = True
                    etat[url] = asdict(annonce)
                else:
                    etat[url] = {"url": url, "ignoree": True}
                pause()
        finally:
            browser.close()
 
    # Marque comme "plus nouvelles" celles déjà connues
    sauver_etat({u: {**d, "nouvelle": d.get("nouvelle", False) and u in dict(a_traiter)}
                 for u, d in etat.items()})
 
    annonces = [Annonce(**d) for d in etat.values() if not d.get("ignoree")]
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
