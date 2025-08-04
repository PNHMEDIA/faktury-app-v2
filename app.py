# app.py - Verze s upraveným formátem názvu a logikou parsování

import os
import json
import base64
import io
from flask import Flask, request, render_template, redirect, url_for, flash, session, send_from_directory, jsonify
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
POPPLER_PATH = os.getenv('POPPLER_PATH')

# --- ROBUSTNÍ NASTAVENÍ CEST PRO UKLÁDÁNÍ SOUBORŮ ---
basedir = os.path.abspath(os.path.dirname(__file__))
STATIC_DIR = os.path.join(basedir, 'static')
UPLOAD_FOLDER = os.path.join(STATIC_DIR, 'uploads')
PROCESSED_FOLDER = os.path.join(STATIC_DIR, 'processed')
PREVIEW_FOLDER = os.path.join(STATIC_DIR, 'previews')
DB_FOLDER = os.path.join(STATIC_DIR, 'db')

app.config.update({
    'UPLOAD_FOLDER': UPLOAD_FOLDER,
    'PROCESSED_FOLDER': PROCESSED_FOLDER,
    'PREVIEW_FOLDER': PREVIEW_FOLDER,
    'DB_FOLDER': DB_FOLDER
})

for folder in [UPLOAD_FOLDER, PROCESSED_FOLDER, PREVIEW_FOLDER, DB_FOLDER]:
    os.makedirs(folder, exist_ok=True)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- POMOCNÉ FUNKCE ---

def get_image_base64(file_path):
    try:
        if file_path.lower().endswith('.pdf'):
            pages = convert_from_path(file_path, 300, first_page=1, last_page=1, poppler_path=POPPLER_PATH or None)
            if not pages: return None
            buf = io.BytesIO()
            pages[0].save(buf, format='JPEG')
            return base64.b64encode(buf.getvalue()).decode('utf-8')
        else:
            with open(file_path, "rb") as image_file:
                return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        print(f"FATAL ERROR v get_image_base64: {e}")
        if "Poppler" in str(e): raise Exception("PopplerNotFound")
        return None

def extract_invoice_data_from_image(base64_image):
    prompt = """
    Jsi vysoce přesný asistent pro české účetnictví. Tvým úkolem je analyzovat obrázek faktury a extrahovat klíčové informace pro automatické přejmenování souboru. Buď maximálně pečlivý.
    Z přiloženého obrázku faktury extrahuj následující informace a vrať je striktně ve formátu JSON:
    1. "supplier_name": Přesný a úplný název dodavatelské firmy. Pokud nenajdeš, vrať "Neznámý dodavatel".
    2. "issue_date": Datum vystavení faktury (nebo DUZP). Formát musí být striktně `RRRR-MM-DD`. Pokud nenajdeš, vrať "RRRR-MM-DD".
    3. "description": Velmi stručný souhrn fakturovaných položek (max 5 slov). Pokud popis nelze určit, vrať "Neznámé zboží".
    4. "detailed_description": Podrobnější popis fakturovaných položek (všechny položky, počty, ceny).
    Vrať pouze a jen validní JSON objekt.
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}],
            response_format={"type": "json_object"},
            max_tokens=500,
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"Chyba při volání OpenAI Vision API: {e}")
        return None

def create_preview(original_path, preview_filename):
    preview_path = os.path.join(app.config['PREVIEW_FOLDER'], preview_filename)
    try:
        if original_path.lower().endswith('.pdf'):
            pages = convert_from_path(original_path, 200, first_page=1, last_page=1, poppler_path=POPPLER_PATH or None)
            if pages: pages[0].save(preview_path, 'JPEG')
        else:
            img = Image.open(original_path)
            img.thumbnail((400, 600))
            img.save(preview_path, 'JPEG')
        return True
    except Exception as e:
        print(f"Nepodařilo se vytvořit náhled: {e}")
        return False

def save_invoice_details(filename, details):
    db_path = os.path.join(app.config['DB_FOLDER'], f"{filename}.json")
    with open(db_path, 'w', encoding='utf-8') as f:
        json.dump(details, f, ensure_ascii=False, indent=4)

def load_invoice_details(filename):
    db_path = os.path.join(app.config['DB_FOLDER'], f"{filename}.json")
    if os.path.exists(db_path):
        with open(db_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def parse_filename(filename):
    base_name = os.path.splitext(filename)[0]
    if base_name.endswith(', E F ZAP'):
        base_name = base_name[:-len(', E F ZAP')]
    
    try:
        parts = base_name.split(' (', 1)
        date_str = parts[0]
        
        remaining = parts[1]
        supplier, description = remaining.split('), (', 1)
        description = description[:-1] # Remove trailing ')'
        
        date_obj = f"20{date_str[0:2]}-{date_str[2:4]}-{date_str[4:6]}"
        return {'date': date_obj, 'supplier': supplier, 'description': description}
    except Exception as e:
        print(f"Chyba při parsování názvu souboru '{filename}': {e}")
        return {'date': 'N/A', 'supplier': 'N/A', 'description': 'N/A'}

# --- ROUTY ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == APP_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        flash('Špatné heslo!', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'): return redirect(url_for('login'))
    
    processed_files_info = []
    sorted_files = sorted(os.listdir(app.config['PROCESSED_FOLDER']), key=lambda f: os.path.getmtime(os.path.join(app.config['PROCESSED_FOLDER'], f)), reverse=True)
    
    for filename in sorted_files:
        parsed_data = parse_filename(filename)
        base_name = os.path.splitext(filename)[0]
        details = load_invoice_details(base_name)
        
        processed_files_info.append({
            'filename': filename,
            'preview_image': f"{base_name}.jpg",
            'date': parsed_data['date'],
            'supplier': parsed_data['supplier'],
            'description': parsed_data['description'],
            'detailed_description': details.get('detailed_description', 'Žádné podrobnosti') if details else 'Žádné podrobnosti'
        })
            
    return render_template('dashboard.html', files=processed_files_info)

@app.route('/upload', methods=['GET', 'POST'])
def upload_page():
    if not session.get('logged_in'): return redirect(url_for('login'))
    if request.method != 'POST': return render_template('upload.html')

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
                if os.path.exists(temp_path): os.remove(temp_path)
                continue

            data = extract_invoice_data_from_image(base64_image)
            if not data:
                flash(f"AI nedokázala extrahovat data ze souboru: {original_filename}", "error")
                continue
            
            date_str = data.get('issue_date', '0000-00-00').replace('-', '')[2:]
            supplier = data.get('supplier_name', 'Neznámý dodavatel').strip()
            description = data.get('description', 'Neznámé zboží').strip()
            
            _, extension = os.path.splitext(original_filename)
            
            # *** PŘESNÝ FORMÁT NÁZVU SOUBORU ***
            base_new_filename = f"{date_str} ({supplier}), ({description}), E F ZAP"
            
            invalid_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
            for char in invalid_chars: base_new_filename = base_new_filename.replace(char, '')
            
            final_filename = f"{base_new_filename.strip()}{extension}"
            processed_path = os.path.join(app.config['PROCESSED_FOLDER'], final_filename)
            
            base_name_for_db = os.path.splitext(final_filename)[0]
            preview_filename = f"{base_name_for_db}.jpg"
            create_preview(temp_path, preview_filename)
            os.rename(temp_path, processed_path)
            
            save_invoice_details(base_name_for_db, data)
            
            flash(f"Faktura '{original_filename}' byla úspěšně zpracována.", "success")

        except Exception as e:
            if "PopplerNotFound" in str(e):
                flash(f"Chyba při zpracování PDF '{original_filename}'. Nástroj Poppler nebyl nalezen.", "error")
            else:
                flash(f"Neznámá chyba při zpracování '{original_filename}'.", "error")
            if os.path.exists(temp_path): os.remove(temp_path)
            continue

    return redirect(url_for('dashboard'))

@app.route('/delete_invoice/<filename>', methods=['POST'])
def delete_invoice(filename):
    if not session.get('logged_in'): return jsonify({'status': 'error', 'message': 'Nepřihlášen'}), 401
    try:
        base_name = os.path.splitext(filename)[0]
        os.remove(os.path.join(app.config['PROCESSED_FOLDER'], filename))
        os.remove(os.path.join(app.config['PREVIEW_FOLDER'], f"{base_name}.jpg"))
        os.remove(os.path.join(app.config['DB_FOLDER'], f"{base_name}.json"))
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/edit_invoice/<filename>', methods=['POST'])
def edit_invoice_submit(filename):
    if not session.get('logged_in'): return jsonify({'status': 'error', 'message': 'Nepřihlášen'}), 401
    
    data = request.json
    new_supplier = data.get('supplier')
    new_description = data.get('description')
    new_date = data.get('date') # Očekává RRRR-MM-DD

    try:
        date_str = new_date.replace('-', '')[2:]
        base_new_filename = f"{date_str} ({new_supplier}), ({new_description}), E F ZAP"
        
        invalid_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
        for char in invalid_chars: base_new_filename = base_new_filename.replace(char, '')
        
        _, extension = os.path.splitext(filename)
        new_filename = f"{base_new_filename.strip()}{extension}"
        
        old_base = os.path.splitext(filename)[0]
        new_base = os.path.splitext(new_filename)[0]

        # Přejmenování všech souvisejících souborů
        os.rename(os.path.join(app.config['PROCESSED_FOLDER'], filename), os.path.join(app.config['PROCESSED_FOLDER'], new_filename))
        os.rename(os.path.join(app.config['PREVIEW_FOLDER'], f"{old_base}.jpg"), os.path.join(app.config['PREVIEW_FOLDER'], f"{new_base}.jpg"))
        os.rename(os.path.join(app.config['DB_FOLDER'], f"{old_base}.json"), os.path.join(app.config['DB_FOLDER'], f"{new_base}.json"))

        return jsonify({'status': 'success', 'new_filename': new_filename})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/previews/<filename>')
def get_preview(filename):
    if not session.get('logged_in'): return redirect(url_for('login'))
    return send_from_directory(app.config['PREVIEW_FOLDER'], filename)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
