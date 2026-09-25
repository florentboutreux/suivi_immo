import os, imaplib, email, re, json, smtplib
from urllib.parse import urlparse, urljoin
from email.message import EmailMessage
from google import genai
from playwright.sync_api import sync_playwright

# Récupération des secrets depuis GitHub Actions
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")

# Configuration de la nouvelle API Gemini
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def get_unread_alerts():
    """Se connecte silencieusement en IMAP pour lire les alertes e-mails."""
    print("Connexion à Gmail pour chercher les alertes...")
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_PASSWORD)
        mail.select("inbox")
        
        # CORRECTION : Commande X-GM-RAW encapsulée en bytes pour éviter les erreurs IMAP
        status, messages = mail.search(None, b'X-GM-RAW', b'"is:unread subject:alerte"')
        urls_trouvees = []
        
        if status == 'OK' and messages[0] != b'':
            for num in messages[0].split():
                status, data = mail.fetch(num, "(RFC822)")
                raw_email = data[0][1]
                
                # NOUVEAU : Liste étendue des domaines (Portails + Agences locales de Millau)
                domaines = r'(leboncoin\.fr|seloger\.com|century21.*\.com|orpi\.com|millau-immobilier\.com|daurelle-immobilier\.com)'
                urls = re.findall(rf'(https?://(?:www\.)?{domaines}[^\s\'"<>]+)', str(raw_email))
                
                if urls:
                    urls_trouvees.append(urls[0])
                
                # Pour marquer le mail comme lu et ne pas le retraiter (décommentez en production)
                # mail.store(num, '+FLAGS', '\\Seen')
                
        mail.logout()
        return list(set(urls_trouvees)) # Retourne une liste sans doublons
    except Exception as e:
        print(f"Erreur lors de la lecture des e-mails : {e}")
        return []

def get_liens_agences_locales():
    """Visite les pages web des agences locales pour extraire les liens d'annonces."""
    
    agences_cibles = [
        "https://www.roques-immobilier.com/",
        "https://www.sga-immobilier.com/immobilier/immobilier-vente-millau.htm",
        "https://www.jmb-immobilier.com/",
        "https://www.immobilier.notaires.fr/fr/annonces-immobilieres/vente/maison/millau-12",
        "https://mesnard-immobilier.com/",
        "https://www.apimmobilier.fr/"
    ]
    
    # Mots-clés génériques pour identifier un lien d'annonce immobilière
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
                # Permet de reconstruire les liens relatifs (ex: /bien/1234 devient https://site.com/bien/1234)
                parsed_url = urlparse(url_recherche)
                racine_site = f"{parsed_url.scheme}://{parsed_url.netloc}"
                
                page.goto(url_recherche, timeout=20000)
                page.wait_for_load_state("networkidle")
                
                # Cherche tous les liens de la page
                liens = page.locator("a").all()
                
                for lien in liens:
                    href = lien.get_attribute("href")
                    if href:
                        href_lower = href.lower()
                        
                        # Filtre heuristique : garde les liens qui ressemblent à des annonces et exclut les pages parasites
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
        response = client.models.generate_content(
            model='gemini-1.5-pro',
            contents=prompt
        )
        return json.loads(response.text.strip('```json\n').strip('```'))
    except Exception as e:
        print(f"Erreur IA : {e}")
        return {"potentiel": False}

def send_report(analyse, url):
    """Génère et envoie le rapport par e-mail."""
    msg = EmailMessage()
    msg['Subject'] = f"🚨 Opportunité MdB - {analyse.get('lots', 0)} lots estimés"
    msg['From'] = GMAIL_USER
    msg['To'] = GMAIL_USER
    
    html = f"""
    <div style="font-family: Arial, sans-serif; color: #333;">
        <h2 style="color: #2c3e50;">🚨 Nouvelle Opportunité Marchand de Biens</h2>
        <div style="background-color: #f8f9fa; padding: 15px; margin-bottom: 20px;">
            <p><b>🧩 Potentiel de découpe :</b> {analyse.get('lots')} lots</p>
            <p><b>📈 Marge estimée :</b> <span style="color: #27ae60; font-weight: bold;">{analyse.get('marge_estimee')} €</span></p>
        </div>
        <h3>Analyse :</h3>
        <p style="padding: 12px; border-left: 4px solid #3498db;">{analyse.get('analyse').replace(chr(10), '<br>')}</p>
        <a href='{url}' style="display:inline-block; padding:10px 15px; background-color:#3498db; color:white; text-decoration:none;">Voir l'annonce complète</a>
    </div>
    """
    msg.set_content(html, subtype='html')
    
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.send_message(msg)
        print("Rapport envoyé avec succès !")

if __name__ == '__main__':
    print("--- Démarrage du Chasseur Immo ---")
    
    # 1. Sources des annonces
    urls_emails = get_unread_alerts()
    urls_agences = get_liens_agences_locales()
    
    # 2. Fusion de toutes les URLs à traiter
    toutes_les_urls = urls_emails + urls_agences
    print(f"{len(toutes_les_urls)} lien(s) à analyser au total.")
    
    # 3. Traitement
    for url in toutes_les_urls:
        texte_annonce = scrape_with_playwright(url)
        if texte_annonce:
            analyse = analyze_deal(texte_annonce)
            
            if analyse.get('potentiel'):
                print(f">>> Opportunité validée sur {url} ! Envoi de l'alerte...")
                send_report(analyse, url)
            else:
                print("Rejeté : Pas de potentiel identifié.")
                
    print("--- Fin du cycle ---")
