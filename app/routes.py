from flask import Flask, render_template, request, send_file, session, redirect, url_for, jsonify, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import requests
import xml.etree.ElementTree as ET
import pandas as pd
import os
import time
from datetime import datetime
from werkzeug.utils import secure_filename
import logging
import re
import shutil
import tempfile
import json
import io
import base64
import random
import numpy as np

# Create Flask app instance
app = Flask(__name__)
app.secret_key = 'hello_world'  # Zorg dat je een sterke geheime sleutel gebruikt in productie

# Configure file upload folder
app.config['UPLOAD_FOLDER'] = 'uploads'

# Make sure the upload folder exists
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

# Configure SQLAlchemy with a SQLite database
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///ecli_cache.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Database model for storing ECLI data
class ECLIEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ecli = db.Column(db.String, unique=True, nullable=False)
    xml_content = db.Column(db.Text, nullable=False)
    identifier_link = db.Column(db.String)
    date_link = db.Column(db.Date)

# Nieuwe database model voor het opslaan van gebruikersinformatie
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    preferences = db.Column(db.JSON, nullable=True)

# Zorg voor deterministisch gedrag in Python
random.seed(0)
np.random.seed(0)

logging.basicConfig(level=logging.INFO)

# Initialize the database within an application context
with app.app_context():
    db.create_all()

# CORS configuratie
CORS(app)

@app.route('/clear_url')
def clear_url():
    session.clear()  # Clear entire session
    return redirect(url_for('welcome'))

@app.route('/')
def welcome():
    return render_template('welcome.html')

@app.route('/faq')
def faq():
    return render_template('faq.html')

@app.route('/contact')
def contact():
    return render_template('contact.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/set_preferences', methods=['POST'])
def set_preferences():
    preferences = request.json.get('preferences', {})
    username = session.get('username', None)
    if username:
        user = User.query.filter_by(username=username).first()
        if user:
            user.preferences = preferences
        else:
            user = User(username=username, preferences=preferences)
            db.session.add(user)
        db.session.commit()
        return jsonify({"status": "success"})
    else:
        return jsonify({"status": "failed", "message": "No user logged in"}), 401

@app.route('/get_dynamic_content', methods=['GET'])
def get_dynamic_content():
    username = session.get('username', None)
    user_data = {}
    if username:
        user = User.query.filter_by(username=username).first()
        if user and user.preferences:
            user_data = user.preferences
    return jsonify({"html": render_template('dynamic_content.html', user_data=user_data)})

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        app.logger.info("Upload POST request received")
        if 'file' not in request.files:
            app.logger.error("No file part in the request")
            return jsonify({"status": "failed", "message": "No file part"})
        
        file = request.files['file']
        if file.filename == '':
            app.logger.error("No selected file")
            return jsonify({"status": "failed", "message": "No selected file"})

        try:
            if file:
                filename = secure_filename(file.filename)
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                app.logger.info(f"Saving file to: {file_path}")
                file.save(file_path)
                
                # Additional processing if needed
                session['file_path'] = file_path
                unique_list = load_eclis_from_excel(file_path)
                session['unique_list'] = unique_list
                app.logger.info(f"Unique ECLI list: {unique_list}")

                return redirect(url_for('loading'))
        except Exception as e:
            app.logger.error(f"Error uploading file: {e}")
            return jsonify({"status": "failed", "message": "File upload failed"})
        
    return render_template('upload.html')

@app.route('/loading')
def loading():
    return render_template('loading.html')

@app.route('/load_data', methods=['POST'])
def load_data():
    unique_list = session.get('unique_list', [])
    app.logger.info(f"Loaded unique_list from session: {unique_list}")
    for ecli in unique_list:
        if not ECLIEntry.query.filter_by(ecli=ecli).first():
            app.logger.info(f"Requesting data for ECLI: {ecli}")
            api_request(ecli)

    # Update de JSON-file na het voltooien van de API-requests
    update_json_file()
    return jsonify({"status": "done"})

@app.route('/choose_analysis')
def choose_analysis():
    return render_template('choose_analysis.html')

@app.route('/traditional_search', methods=['GET', 'POST'])
def traditional_search():
    if request.method == 'POST':
        search_terms = request.form.get('search_terms', '').strip()
        include_synonyms = 'include_synonyms' in request.form
        session['search_terms'] = search_terms
        session['include_synonyms'] = include_synonyms
        return redirect(url_for('traditional_analysis'))
    return render_template('traditional_search.html')

@app.route('/traditional_analysis', methods=['GET', 'POST'])
def traditional_analysis():
    search_results_count = 0
    used_synonyms = {}

    search_terms = session.get('search_terms', '')
    include_synonyms = session.get('include_synonyms', False)
    ECLI_texts = session.get('ECLI_texts', {})  # Load ECLI_texts from session

    start_time = time.time()

    if search_terms:
        search_terms = [term.split('|') for term in search_terms.split(',') if term]

        if include_synonyms:
            extended_search_terms = []
            for term_group in search_terms:
                extended_group = set(term_group)
                for term in term_group:
                    synonyms = get_synonyms(term)
                    extended_group.update(synonyms)
                    if term not in used_synonyms:
                        used_synonyms[term] = set()
                    used_synonyms[term].update(synonyms)
                extended_search_terms.append(list(extended_group))
            search_terms = extended_search_terms

        ECLI_texts = {}

        unique_list = session.get('unique_list', [])
        for ecli in unique_list:
            root, identifier_link, date_link = api_request(ecli)
            if root is None:
                continue
            texts = [str(elem.text) for elem in root.iter() if elem.text and all(
                any(synonym.lower() in str(elem.text).lower() for synonym in term)
                for term in search_terms
            )]
            highlighted_texts = []
            for text in texts:
                for term_group in search_terms:
                    for synonym in term_group:
                        text = highlight_term(text, synonym)
                highlighted_texts.append(text)
            ECLI_texts[ecli] = {'texts': highlighted_texts, 'identifier_link': identifier_link, 'current_index': 0, 'date_link': date_link}
            search_results_count += len(highlighted_texts)

        session['used_synonyms'] = {term: list(synonyms) for term, synonyms in used_synonyms.items()}
        session['ECLI_texts'] = ECLI_texts  # Save ECLI_texts in session
        session.modified = True

    update_excel_file()
    update_json_file()

    scroll_position = session.get('scroll_position', 0)
    app.logger.info(f"ECLI_texts: {ECLI_texts}")

    end_time = time.time()
    elapsed_time = end_time - start_time
    app.logger.info(f"Traditional Search tijd: {elapsed_time} seconden")

    emissions_traditional = session.get('emissions_traditional', 0)
    
    if emissions_traditional is None:
        emissions_traditional = 0

    emissions_data = [emissions_traditional, 0]

    session['current_analysis'] = 'traditional'  # Mark that traditional analysis is active

    return render_template('traditional_analysis.html', ECLI_texts=ECLI_texts, search_results_count=search_results_count, used_synonyms=used_synonyms, search_terms=search_terms, scroll_position=scroll_position, emissions_data=emissions_data)

@app.route('/previous_ecli/<ecli>', methods=['GET'])
def previous_ecli(ecli):
    ECLI_texts = session.get('ECLI_texts', {})
    logging.info(f"Previous ECLI called for: {ecli}")
    if ecli in ECLI_texts:
        current_index = ECLI_texts[ecli]['current_index']
        ECLI_texts[ecli]['current_index'] = min(len(ECLI_texts[ecli]['texts']) - 1, current_index + 1)
        logging.info(f"Updated index for {ecli}: {ECLI_texts[ecli]['current_index']}")
        session['ECLI_texts'] = ECLI_texts  # Update session with new ECLI_texts
        session.modified = True
    update_excel_file()
    update_json_file()

    text = ECLI_texts[ecli]['texts'][ECLI_texts[ecli]['current_index']] if ECLI_texts[ecli]['texts'] else 'No results'
    return jsonify({
        'text': text,
        'index': ECLI_texts[ecli]['current_index'] + 1,
        'length': len(ECLI_texts[ecli]['texts'])
    })

@app.route('/next_ecli/<ecli>', methods=['GET'])
def next_ecli(ecli):
    ECLI_texts = session.get('ECLI_texts', {})
    logging.info(f"Next ECLI called for: {ecli}")
    if ecli in ECLI_texts:
        current_index = ECLI_texts[ecli]['current_index']
        ECLI_texts[ecli]['current_index'] = max(0, current_index - 1)
        logging.info(f"Updated index for {ecli}: {ECLI_texts[ecli]['current_index']}")
        session['ECLI_texts'] = ECLI_texts  # Update session with new ECLI_texts
        session.modified = True
    update_excel_file()
    update_json_file()

    text = ECLI_texts[ecli]['texts'][ECLI_texts[ecli]['current_index']] if ECLI_texts[ecli]['texts'] else 'No results'
    return jsonify({
        'text': text,
        'index': ECLI_texts[ecli]['current_index'] + 1,
        'length': len(ECLI_texts[ecli]['texts'])
    })

@app.route('/delete_ecli/<ecli>', methods=['GET'])
def delete_ecli(ecli):
    ECLI_texts = session.get('ECLI_texts', {})
    logging.info(f"Delete ECLI called for: {ecli}")
    if ecli in ECLI_texts:
        current_index = ECLI_texts[ecli]['current_index']
        if ECLI_texts[ecli]['texts']:
            del ECLI_texts[ecli]['texts'][current_index]
            if current_index >= len(ECLI_texts[ecli]['texts']):
                ECLI_texts[ecli]['current_index'] = max(0, len(ECLI_texts[ecli]['texts']) - 1)
            ECLI_texts[ecli]['texts'] = [text for text in ECLI_texts[ecli]['texts'] if text]
        logging.info(f"Deleted text for {ecli}, remaining texts: {len(ECLI_texts[ecli]['texts'])}")
        session['ECLI_texts'] = ECLI_texts  # Update session with new ECLI_texts
        session.modified = True
    update_excel_file()
    update_json_file()

    return jsonify({
        'text': 'No results' if not ECLI_texts[ecli]['texts'] else ECLI_texts[ecli]['texts'][ECLI_texts[ecli]['current_index']],
        'index': ECLI_texts[ecli]['current_index'] + 1,
        'length': len(ECLI_texts[ecli]['texts'])
    })

@app.route('/download_template')
def download_template():
    return send_from_directory(directory='static', path='ecli_template.xlsx', as_attachment=True)

@app.route('/download/excel', methods=['GET'])
def download_excel():
    current_analysis = session.get('current_analysis', None)
    temp_excel_file = session.get('temp_excel_file', None)

    if current_analysis == 'traditional' and temp_excel_file:
        return send_file(
            temp_excel_file,
            as_attachment=True,
            download_name='ECLI_results.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    else:
        return "No Excel file generated", 404
    
@app.route('/download/json', methods=['GET'])
def download_json():
    current_analysis = session.get('current_analysis', None)
    temp_json_file = session.get('temp_json_file', None)

    if current_analysis == 'traditional' and temp_json_file:
        return send_file(
            temp_json_file,
            as_attachment=True,
            download_name='ECLI_results.json',
            mimetype='application/json'
        )
    else:
        return "No JSON file generated", 404

def highlight_term(text, term):
    if text is None:
        return text
    if not isinstance(text, str):
        text = str(text)
    return re.sub(r'(?i)('+re.escape(term)+r')', r'<span class="highlight">\1</span>', text)

def update_excel_file():
    data = []
    ECLI_texts = session.get('ECLI_texts', {})
    for ecli, texts in ECLI_texts.items():
        date = texts.get('date_link', 'No date available')
        if texts['texts']:
            current_text = remove_html_tags(texts['texts'][texts['current_index']])
            result_text = remove_html_tags(current_text)
        else:
            result_text = 'none'
        link = texts.get('identifier_link', 'No link available')

        data.append((date, ecli, link, result_text))

    df = pd.DataFrame(data, columns=['Date', 'ECLI', 'Link', 'Result'])

    with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as temp_file:
        df.to_excel(temp_file.name, index=False)

    session['temp_excel_file'] = temp_file.name
    session.modified = True

def update_json_file():
    data = []
    for ecli in ECLIEntry.query.all():
        root = ET.fromstring(ecli.xml_content)
        identifier_link = ecli.identifier_link
        date_link = ecli.date_link

        texts = [elem.text for elem in root.iter() if elem.text]

        data.append({
            'ecli': ecli.ecli,
            'identifier_link': identifier_link,
            'date_link': date_link,
            'texts': texts
        })

    with tempfile.NamedTemporaryFile(delete=False, suffix='.json', mode='w', encoding='utf-8') as temp_file:
        json.dump(data, temp_file, indent=4, default=str)
        session['temp_json_file'] = temp_file.name
    session.modified = True

def load_eclis_from_excel(file_path):
    try:
        logging.info(f"Loading ECLIs from file: {file_path}")
        df = pd.read_excel(file_path)
        if 'ECLI' not in df.columns:
            raise ValueError("Excel file must contain an 'ECLI' column.")
        eclis = df['ECLI'].dropna().tolist()
        logging.info(f"Successfully loaded {len(eclis)} ECLIs from file.")
        return eclis
    except Exception as e:
        logging.error(f"Error loading ECLIs from file: {e}")
        return []

def clean_ecli(ecli):
    try:
        cleaned_ecli = ecli.split(' ')[0].strip()
        if not cleaned_ecli.startswith('ECLI:'):
            raise ValueError(f"Invalid ECLI format: {ecli}")
        logging.info(f"Cleaned ECLI: {cleaned_ecli} from original ECLI: {ecli}")
        return cleaned_ecli
    except Exception as e:
        logging.error(f"Error cleaning ECLI {ecli}: {e}")
        return None

def api_request(ecli):
    cleaned_ecli = clean_ecli(ecli)
    namespaces = {
        'rdf': "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        'dcterms': "http://purl.org/dc/terms/",
    }

    logging.info(f"Requesting data for ECLI: {cleaned_ecli}")

    entry = ECLIEntry.query.filter_by(ecli=cleaned_ecli).first()
    if entry:
        root = ET.fromstring(entry.xml_content)
        identifier_link = entry.identifier_link
        date_link = entry.date_link
    else:
        url = f"https://data.rechtspraak.nl/uitspraken/content?id={cleaned_ecli}"
        try:
            response = requests.get(url, stream=True)
            logging.info(f"API Response status code: {response.status_code}")
            response.raise_for_status()
            root = ET.fromstring(response.content)

            identifier_link = None
            for identifier_tag in root.findall('.//rdf:Description/dcterms:identifier', namespaces):
                if identifier_tag.text and identifier_tag.text.startswith('http'):
                    identifier_link = identifier_tag.text
                    break

            date_link = None
            for date_tag in root.findall('.//rdf:Description/dcterms:issued', namespaces):
                if date_tag.text:
                    try:
                        date_link = datetime.strptime(date_tag.text, '%Y-%m-%d').date()
                    except ValueError:
                        logging.warning(f"Invalid date format for ECLI {cleaned_ecli}: {date_tag.text}")
                        date_link = None

            new_entry = ECLIEntry(
                ecli=cleaned_ecli,
                xml_content=ET.tostring(root, encoding='unicode'),
                identifier_link=identifier_link,
                date_link=date_link
            )
            db.session.add(new_entry)
            db.session.commit()

        except requests.exceptions.RequestException as e:
            logging.error(f"Error fetching ECLI data for {cleaned_ecli}: {e}")
            return None, None, None

    return root, identifier_link, date_link

def remove_html_tags(text):
    if text is None:
        return ''
    if not isinstance(text, str):
        text = str(text)
    clean = re.compile('<.*?>')
    return re.sub(clean, '', text)

def get_synonyms(word):
    return [word]

if __name__ == '__main__':
    app.run(debug=False, port=5000)
