import os, imaplib, email, re, json, smtplib
from email.message import EmailMessage
from google import genai
from playwright.sync_api import sync_playwright

# Récupération des secrets depuis GitHub
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")

# Nouvelle syntaxe pour l'API Gemini
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def get_unread_alerts():
    """Se connecte silencieusement en IMAP pour lire les alertes"""
    print("Connexion à Gmail...")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_PASSWORD)
    mail.select("inbox")
    
    # CORRECTION : Syntaxe séparée pour la recherche Gmail
    status, messages = mail.search(None, 'X-GM-RAW', 'is:unread subject:alerte')
    urls_trouvees = []
    
    # On vérifie si la recherche a trouvé des emails
    if status == 'OK' and messages[0]:
        for num in messages[0].split():
            status, data = mail.fetch(num, "(RFC822)")
            raw_email = data[0][1]
            urls = re.findall(r'(https?://(?:www\.)?(?:leboncoin\.fr|seloger\.com)[^\s\'"<>]+)', str(raw_email))
            if urls:
                urls_trouvees.append(urls[0])
            # Décommenter pour marquer comme lu en production :
            # mail.store(num, '+FLAGS', '\\Seen')
            
    mail.logout()
    return list(set(urls_trouvees))

def scrape_with_playwright(url):
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
            print(f"Erreur Playwright : {e}")
            browser.close()
            return None

def analyze_deal(texte):
    prompt = f"""
    Expert marchand de biens. Analyse cette annonce.
    Cherche le potentiel de division (immeuble, découpe).
    JSON attendu : {{"potentiel": bool, "lots": int, "analyse": "texte", "marge_estimee": int}}
    Annonce : {texte}
    """
    try:
        # CORRECTION : Nouvel appel à l'API Gemini
        response = client.models.generate_content(
            model='gemini-1.5-pro',
            contents=prompt
        )
        return json.loads(response.text.strip('```json\n').strip('```'))
    except Exception as e:
        print(f"Erreur IA : {e}")
        return {"potentiel": False}

def send_report(analyse, url):
    """Envoie l'e-mail via SMTP"""
    msg = EmailMessage()
    msg['Subject'] = f"🚨 Opportunité MdB - {analyse.get('lots', 0)} lots"
    msg['From'] = GMAIL_USER
    msg['To'] = GMAIL_USER
    
    html = f"<h3>Potentiel: {analyse.get('lots')} lots | Marge: {analyse.get('marge_estimee')}€</h3><p>{analyse.get('analyse')}</p><a href='{url}'>Voir l'annonce</a>"
    msg.set_content(html, subtype='html')
    
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.send_message(msg)
        print("Rapport envoyé !")

if __name__ == '__main__':
    urls = get_unread_alerts()
    print(f"{len(urls)} alertes trouvées.")
    for url in urls:
        texte = scrape_with_playwright(url)
        if texte:
            analyse = analyze_deal(texte)
            if analyse.get('potentiel'):
                send_report(analyse, url)
