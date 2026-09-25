import os, re, json
from urllib.parse import urlparse, urljoin
from google import genai
from playwright.sync_api import sync_playwright

# Configuration de l'API Gemini
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def get_liens_agences_locales():
    """Visite les pages web des agences locales pour extraire les liens d'annonces."""
    agences_cibles = [
  
        "https://www.apimmobilier.fr/"
    ]
    
    # Mots-clés pour filtrer les URLs pertinentes
    mots_cles_annonces = ["/vente/", "/annonce/", "/bien/", "/detail/", "/maison/", "/appartement/"]
    mots_cles_exclus = ["contact", "mentions", "honoraires", "estimation", "agence", "actualites"]
    
    urls_trouvees = []
    print("Scraping direct des agences locales...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        for url_recherche in agences_cibles:
            print(f"Analyse du site : {url_recherche}")
            try:
                parsed_url = urlparse(url_recherche)
                racine_site = f"{parsed_url.scheme}://{parsed_url.netloc}"
                
                page.goto(url_recherche, timeout=20000)
                page.wait_for_load_state("networkidle")
                
                liens = page.locator("a").all()
                
                for lien in liens:
                    href = lien.get_attribute("href")
                    if href:
                        href_lower = href.lower()
                        if any(mot in href_lower for mot in mots_cles_annonces) and not any(exclu in href_lower for exclu in mots_cles_exclus):
                            lien_complet = urljoin(racine_site, href)
                            urls_trouvees.append(lien_complet)
                            
            except Exception as e:
                print(f"Erreur lors du scraping de {url_recherche} : {e}")
                
        browser.close()
        return list(set(urls_trouvees))

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

def analyze_deal(texte):
    """Demande à Gemini d'évaluer le potentiel Marchand de Biens."""
    prompt = f"""
    Tu es un expert marchand de biens. Analyse cette annonce.
    Cherche le potentiel de division (immeuble, découpe).
    JSON attendu : {{"potentiel": bool, "lots": int, "analyse": "texte", "marge_estimee": int}}
    Annonce : {texte}
    """
    try:
        # Utilisation de l'alias pointant vers la dernière version Pro disponible
        response = client.models.generate_content(
            model='gemini-1.5-pro-latest', 
            contents=prompt
        )
        return json.loads(response.text.strip('```json\n').strip('```'))
    except Exception as e:
        print(f"Erreur IA : {e}")
        return {"potentiel": False}

def generate_html_report(analyses_validees):
    """Génère un fichier HTML listant toutes les opportunités validées."""
    html_content = """
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Tableau de Bord - Chasseur Immo</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-50 p-8 font-sans">
        <div class="max-w-7xl mx-auto">
            <header class="mb-10 border-b border-slate-200 pb-6">
                <h1 class="text-4xl font-extrabold text-slate-900 tracking-tight">🚨 Opportunités de Division</h1>
                <p class="text-slate-500 mt-2 text-lg">Analyse automatisée des annonces locales</p>
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
        html_content += f"""
                <div class="bg-white p-6 rounded-xl shadow-sm hover:shadow-md transition-shadow border border-slate-200 flex flex-col justify-between">
                    <div>
                        <div class="flex justify-between items-start mb-4">
                            <span class="bg-indigo-100 text-indigo-800 text-sm font-semibold px-3 py-1 rounded-full">{deal['analyse'].get('lots', 0)} lots estimés</span>
                            <span class="text-emerald-600 font-bold text-lg">{deal['analyse'].get('marge_estimee', 'N/A')} € marge</span>
                        </div>
                        <p class="text-slate-700 text-sm mb-6 leading-relaxed">{deal['analyse'].get('analyse', '')}</p>
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
    print("--- Démarrage du Chasseur Immo (Génération de Dashboard) ---")
    
    urls_agences = get_liens_agences_locales()
    print(f"{len(urls_agences)} lien(s) à analyser au total.\n")
    
    opportunites_trouvees = []
    
    for url in urls_agences:
        texte_annonce = scrape_with_playwright(url)
        if texte_annonce:
            analyse = analyze_deal(texte_annonce)
            
            if analyse.get('potentiel'):
                print(f"[!] Opportunité validée : {url}")
                opportunites_trouvees.append({
                    "url": url,
                    "analyse": analyse
                })
            else:
                print(f"Rejeté (Pas de potentiel) : {url}")
                
    generate_html_report(opportunites_trouvees)
    print("\n--- Fin du cycle ---")
