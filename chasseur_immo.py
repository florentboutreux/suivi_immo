import os, json, time
from urllib.parse import urlparse, urljoin
from google import genai
from playwright.sync_api import sync_playwright

# Initialisation de la bibliothèque officielle et moderne
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def get_liens_agences_locales():
    """Visite les agences locales, explore plusieurs pages de résultats (pagination) et extrait les vraies fiches."""
    agences_cibles = [
        "https://www.roques-immobilier.com/",
        "https://www.sga-immobilier.com/immobilier/immobilier-vente-millau.htm",
        "https://www.jmb-immobilier.com/",
        "https://www.immobilier.notaires.fr/fr/annonces-immobilieres/vente/maison/millau-12",
        "https://mesnard-immobilier.com/vente/",
        "https://www.apimmobilier.fr/recherche/"
    ]
    
    mots_cles_annonces = ["/annonce/", "/bien/", "/detail/", "/vente-", "/p-r7-", "mandat"]
    mots_cles_exclus = [
        "contact", "mentions", "honoraires", "estimation", "agence", "actualites", 
        "property-type", "recherche", "filter", "connexion", "espace-client"
    ]
    
    urls_trouvees = []
    print("Scraping intelligent et multipage des agences locales...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        for url_base in agences_cibles:
            print(f"\nAnalyse de l'agence : {url_base}")
            
            # On simule une navigation sur plusieurs pages (ex: jusqu'à 3 pages de résultats par site)
            for page_num in range(1, 4):
                # Construction de l'URL selon la pagination (gère les cas courants)
                if page_num == 1:
                    url_courante = url_base
                else:
                    # Ajustement standard pour les pages suivantes
                    if "?" in url_base:
                        url_courante = f"{url_base}&page={page_num}"
                    else:
                        url_courante = f"{url_base.rstrip('/')}/page/{page_num}/"
                
                try:
                    print(f" -> Chargement page {page_num} : {url_courante}")
                    page.goto(url_courante, timeout=25000)
                    page.wait_for_load_state("networkidle")
                    
                    # Scroll pour forcer le chargement dynamique (lazy loading)
                    for _ in range(2):
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(1000)
                    
                    # Extraction des liens
                    liens = page.locator("a").all()
                    avant_count = len(urls_trouvees)
                    
                    parsed_url = urlparse(url_courante)
                    racine_site = f"{parsed_url.scheme}://{parsed_url.netloc}"
                    
                    for lien in liens:
                        href = lien.get_attribute("href")
                        if href:
                            href_lower = href.lower()
                            est_une_annonce = any(mot in href_lower for mot in mots_cles_annonces)
                            est_exclu = any(exclu in href_lower for exclu in mots_cles_exclus)
                            
                            if est_une_annonce and not est_exclu:
                                lien_complet = urljoin(racine_site, href)
                                urls_trouvees.append(lien_complet)
                    
                    # Si la page ne donne plus aucun nouveau lien, ça veut dire qu'on a dépassé la dernière page, on stoppe la pagination pour ce site
                    if len(urls_trouvees) == avant_count and page_num > 1:
                        print(" -> Fin de pagination atteinte pour ce site.")
                        break
                        
                except Exception as e:
                    # Si la page 2 ou 3 n'existe pas (erreur 404 ou timeout), on passe simplement au site suivant
                    print(f" -> Fin ou pas de page {page_num} ({e})")
                    break
                
        browser.close()
        
    liens_uniques = list(set(urls_trouvees))
    print(f"\n✨ Total cumulé : {len(liens_uniques)} fiches d'annonces uniques détectées sur l'ensemble des agences.")
    return liens_uniques

def scrape_with_playwright(url):
    """Ouvre l'annonce dans un navigateur invisible et extrait le texte."""
    print(f"Extraction Playwright: {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=20000)
            page.wait_for_load_state("networkidle")
            texte = page.inner_text("body")
            browser.close()
            return texte
        except Exception as e:
            print(f"Erreur Playwright sur {url}: {e}")
            browser.close()
            return None

def analyze_deal(texte_annonce, url):
    """Analyse l'annonce avec Gemini en mode gratuit (avec le modèle 3.8 requis)."""
    prompt = f"""
    Tu es l'agent d'acquisition IA pour la SAS FBIMMO. 
    Ta spécialité : identifier des immeubles ou grandes maisons avec un potentiel de division spatiale pour revente à la découpe.
    
    REGLE ABSOLUE : Tu dois mener une réflexion critique dans des balises <brouillon> avant de donner ton verdict final en JSON.
    
    Format attendu :
    <brouillon>
    1. HYPOTHÈSE : Surface, potentiel théorique de lots.
    2. AUTO-CRITIQUE : Freins techniques (accès, compteurs, lumière, faisabilité).
    3. AJUSTEMENT : Meilleure configuration de découpe.
    </brouillon>
    {{
      "potentiel": true,
      "lots": 3,
      "marge_estimee": 50000,
      "analyse_finale": "texte synthétique"
    }}
    
    Annonce à analyser : {texte_annonce}
    """

    print(f"Analyse Gemini en cours pour {url}...")
    
    try:
        # Utilisation du modèle actuel exigé par l'API
        response = client.models.generate_content(
            model='gemini-3.8-flash', 
            contents=prompt
        )
        
        reponse_complete = response.text
        
        if "</brouillon>" in reponse_complete:
            json_brut = reponse_complete.split("</brouillon>")[-1].strip()
        else:
            json_brut = reponse_complete.strip()
            
        json_propre = json_brut.strip('```json\n').strip('```').strip()
        return json.loads(json_propre)
        
    except Exception as e:
        print(f"Erreur IA ou formatage JSON : {e}")
        return {"potentiel": False}

def generate_html_report(analyses_validees):
    """Génère un fichier HTML listant toutes les opportunités validées."""
    html_content = """
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Tableau de Bord - FBIMMO</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-50 p-8 font-sans">
        <div class="max-w-7xl mx-auto">
            <header class="mb-10 border-b border-slate-200 pb-6">
                <h1 class="text-4xl font-extrabold text-slate-900 tracking-tight">🚨 Opportunités de Division</h1>
                <p class="text-slate-500 mt-2 text-lg">Agent Autonome FBIMMO - Analyse avec Google Gemini</p>
            </header>
            
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-8">
    """
    
    if not analyses_validees:
        html_content += """
                <div class="col-span-full bg-white p-8 rounded-xl shadow-sm border border-slate-200 text-center">
                    <p class="text-slate-500 text-lg font-medium">Aucune opportunité à fort potentiel détectée lors de ce scan.</p>
                </div>
        """
    
    for deal in analyses_validees:
        lots = deal['analyse'].get('lots', 0)
        marge = deal['analyse'].get('marge_estimee', 'N/A')
        analyse_texte = deal['analyse'].get('analyse_finale', '')
        
        html_content += f"""
                <div class="bg-white p-6 rounded-xl shadow-sm hover:shadow-md transition-shadow border border-slate-200 flex flex-col justify-between">
                    <div>
                        <div class="flex justify-between items-start mb-4">
                            <span class="bg-indigo-100 text-indigo-800 text-sm font-semibold px-3 py-1 rounded-full">{lots} lots estimés</span>
                            <span class="text-emerald-600 font-bold text-lg">{marge} € marge</span>
                        </div>
                        <p class="text-slate-700 text-sm mb-6 leading-relaxed">{analyse_texte}</p>
                    </div>
                    <a href="{deal['url']}" target="_blank" class="w-full text-center bg-slate-900 hover:bg-slate-800 text-white font-medium py-2.5 px-4 rounded-lg transition-colors">
                        Voir l'annonce complète
                    </a>
                </div>
        """
        
    html_content += """
            </div>
        </div>
    </body>
    </html>
    """
    
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("\n>>> Rapport HTML généré avec succès : index.html")

if __name__ == '__main__':
    print("--- Réveil de l'Agent Autonome FBIMMO (Mode Gratuit) ---")
    
    urls_agences = get_liens_agences_locales()
    print(f"{len(urls_agences)} lien(s) à analyser au total.\n")
    
    opportunites_trouvees = []
    
    for url in urls_agences:
        texte_annonce = scrape_with_playwright(url)
        if texte_annonce:
            analyse = analyze_deal(texte_annonce, url)
            
            if analyse.get('potentiel'):
                print(f"[!] Opportunité validée : {url}")
                opportunites_trouvees.append({
                    "url": url,
                    "analyse": analyse
                })
            else:
                print(f"Rejeté (Pas de potentiel) : {url}")
            
            # Pause de 15 secondes pour respecter le quota gratuit
            print("⏳ Pause de 15s (respect du quota gratuit)...")
            time.sleep(15)
                
    generate_html_report(opportunites_trouvees)
    print("\n--- Fin du cycle ---")
