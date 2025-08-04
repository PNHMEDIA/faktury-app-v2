# app.py - Finální verze aplikace pro zpracování faktur
# Využívá OpenAI Vision API a je kompatibilní s lokálním i cloudovým nasazením

import os
import json
import base64
import io
from flask import Flask, request, render_template, redirect, url_for, flash, session, send_from_directory
from werkzeug.utils import secure_filename
from PIL import Image
from openai import OpenAI
from pdf2image import convert_from_path
from dotenv import load_dotenv

# Načtení proměnných z .env souboru
load_dotenv()

# --- NASTAVENÍ APLIKACE ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY')
APP_PASSWORD = os.getenv('APP_PASSWORD')
POPPLER_PATH = os.getenv('POPPLER_PATH') # Cesta k Poppler pro lokální vývoj

# Cesty ke složkám
UPLOAD_FOLDER = 'static/uploads'
PROCESSED_FOLDER = 'static/processed'
PREVIEW_FOLDER = 'static/previews'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['PROCESSED_FOLDER'] = PROCESSED_FOLDER
app.config['PREVIEW_FOLDER'] = PREVIEW_FOLDER

# Vytvoření složek, pokud neexistují
for folder in [UPLOAD_FOLDER, PROCESSED_FOLDER, PREVIEW_FOLDER]:
    if not os.path.exists(folder):
        os.makedirs(folder)

# Inicializace OpenAI klienta
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- POMOCNÉ FUNKCE ---

def get_image_base64(file_path):
    """
    Převede soubor (obrázek nebo první stránku PDF) na base64-enkódovaný řetězec.
    """
    try:
        if file_path.lower().endswith('.pdf'):
            # Použije cestu k Poppler, pouze pokud je explicitně definována
            pages = convert_from_path(file_path, 300, first_page=1, last_page=1, poppler_path=POPPLER_PATH or None)
            if not pages:
                return None
            
            buf = io.BytesIO()
            pages[0].save(buf, format='JPEG')
            image_bytes = buf.getvalue()
        else:
            with open(file_path, "rb") as image_file:
                image_bytes = image_file.read()

        return base64.b64encode(image_bytes).decode('utf-8')
    except Exception as e:
        print(f"FATAL ERROR v get_image_base64: {e}")
        if "Poppler" in str(e):
            raise Exception("PopplerNotFound")
        return None

def extract_invoice_data_from_image(base64_image):
    """
    Pošle obrázek na OpenAI Vision API a požádá o extrakci dat.
    """
    prompt = """
    Jsi vysoce přesný asistent pro české účetnictví. Tvým úkolem je analyzovat obrázek faktury a extrahovat klíčové informace pro automatické přejmenování souboru. Buď maximálně pečlivý.

    Z přiloženého obrázku faktury extrahuj následující informace a vrať je striktně ve formátu JSON:

    1.  "supplier_name": Najdi přesný a úplný název dodavatelské firmy (protistrany), která fakturu vystavila. Hledej pole označená jako "Dodavatel" nebo "Vystavil". Pokud název nenajdeš, vrať "Neznámý dodavatel".

    2.  "issue_date": Najdi datum vystavení faktury. Hledej klíčová slova jako "Datum vystavení", "Datum zdanitelného plnění" (DUZP) nebo "Datum uskutečnění plnění". Vždy vrať nejrelevantnější datum pro účetnictví. Formát musí být striktně `RRRR-MM-DD`. Pokud datum nenajdeš, vrať "RRRR-MM-DD".

    3.  "description": Identifikuj hlavní předmět fakturace. Podívej se na seznam fakturovaných položek a vytvoř z nich velmi stručný souhrn (maximálně 4-5 slov). Například: "Nákup kancelářských potřeb", "Hostingové služby za květen", "Marketingová konzultace", "Nákup piva". Pokud popis nelze určit, vrať "Neznámé zboží".

    Vrať pouze a jen validní JSON objekt. Žádný další text před nebo za ním.
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            },
                        },
                    ],
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=300,
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"Chyba při volání OpenAI Vision API: {e}")
        return None

def create_preview(original_path, preview_filename):
    """Vytvoří obrázkový náhled pro dashboard."""
    preview_path = os.path.join(app.config['PREVIEW_FOLDER'], preview_filename)
    try:
        if original_path.lower().endswith('.pdf'):
            pages = convert_from_path(original_path, 200, first_page=1, last_page=1, poppler_path=POPPLER_PATH or None)
            if pages:
                pages[0].save(preview_path, 'JPEG')
        else:
            img = Image.open(original_path)
            img.thumbnail((400, 600))
            img.save(preview_path, 'JPEG')
        return True
    except Exception as e:
        print(f"Nepodařilo se vytvořit náhled: {e}")
        return False

# --- ROUTY (JEDNOTLIVÉ STRÁNKY) ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == APP_PASSWORD:
            session['logged_in'] = True
            flash('Přihlášení bylo úspěšné!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Špatné heslo!', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash('Byli jste odhlášeni.', 'info')
    return redirect(url_for('login'))

@app.route('/')
@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    
    processed_files_info = []
    # Seřadí soubory, aby se nejnovější zobrazovaly nahoře
    sorted_files = sorted(os.listdir(app.config['PROCESSED_FOLDER']), key=lambda f: os.path.getmtime(os.path.join(app.config['PROCESSED_FOLDER'], f)), reverse=True)
    
    for filename in sorted_files:
        try:
            base_name = os.path.splitext(filename)[0]
            
            if base_name.endswith(', E F ZAP'):
                base_name = base_name[:-len(', E F ZAP')]

            parts = base_name.split(', ', 1)
            date_and_supplier_part = parts[0]
            description = parts[1] if len(parts) > 1 else "Neznámý popis"
            
            date_supplier_split = date_and_supplier_part.split(' ', 1)
            date_str = date_supplier_split[0]
            supplier = date_supplier_split[1] if len(date_supplier_split) > 1 else "Neznámý dodavatel"

            preview_filename = os.path.splitext(filename)[0] + ".jpg"
            processed_files_info.append({
                'filename': filename, 'preview_image': preview_filename,
                'date': f"20{date_str[0:2]}-{date_str[2:4]}-{date_str[4:6]}",
                'supplier': supplier, 'description': description
            })
        except Exception as e:
            print(f"Chyba při parsování názvu souboru '{filename}': {e}")
            continue
            
    return render_template('dashboard.html', files=processed_files_info)

@app.route('/upload', methods=['GET', 'POST'])
def upload_page():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    if request.method == 'POST':
        files = request.files.getlist('files')
        if not files or files[0].filename == '':
            flash("Nebyly vybrány žádné soubory.", "warning")
            return redirect(url_for('upload_page'))

        for file in files:
            original_filename = secure_filename(file.filename)
            temp_path = os.path.join(app.config['UPLOAD_FOLDER'], original_filename)
            file.save(temp_path)

            try:
                base64_image = get_image_base64(temp_path)
                if not base64_image:
                    flash(f"Nepodařilo se zpracovat soubor na obrázek: {original_filename}", "error")
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    continue

                data = extract_invoice_data_from_image(base64_image)
                if not data:
                    flash(f"AI nedokázala extrahovat data ze souboru: {original_filename}", "error")
                    continue
                
                date_str = data.get('issue_date', 'RRRR-MM-DD').replace('-', '')[2:]
                supplier = data.get('supplier_name', 'Neznámý dodavatel').strip()
                description = data.get('description', 'Neznámé zboží').strip()
                
                _, extension = os.path.splitext(original_filename)
                
                base_new_filename = f"{date_str} {supplier}, {description}, E F ZAP"
                
                invalid_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
                for char in invalid_chars:
                    base_new_filename = base_new_filename.replace(char, '')
                
                final_filename = f"{base_new_filename.strip()}{extension}"
                processed_path = os.path.join(app.config['PROCESSED_FOLDER'], final_filename)
                
                preview_filename = os.path.splitext(final_filename)[0] + ".jpg"
                create_preview(temp_path, preview_filename)
                os.rename(temp_path, processed_path)
                flash(f"Faktura '{original_filename}' byla úspěšně zpracována.", "success")

            except Exception as e:
                if "PopplerNotFound" in str(e):
                    flash(f"Chyba při zpracování PDF '{original_filename}'. Nástroj Poppler nebyl nalezen. Zkontrolujte, zda je správně nainstalován (např. přes Homebrew na Macu).", "error")
                else:
                    flash(f"Vyskytla se neznámá chyba při zpracování souboru '{original_filename}'. Zkontrolujte terminál pro detaily.", "error")
                
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                continue

        return redirect(url_for('dashboard'))

    return render_template('upload.html')

@app.route('/previews/<filename>')
def get_preview(filename):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return send_from_directory(app.config['PREVIEW_FOLDER'], filename)


if __name__ == '__main__':
    # Úprava pro nasazení na Render
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
