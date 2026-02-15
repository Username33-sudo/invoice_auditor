"""
Скрипт аудита счетов-фактур по электроэнергии с использованием GigaChat API.
Версия 1.1 - исправлена обработка JSON ответов от GigaChat

Установка зависимостей:
pip install requests python-dotenv pdf2image pytesseract PyPDF2 opencv-python numpy pillow
"""

import os
import sys
import io
import re
import shutil
import time
import uuid
import json
from datetime import datetime, timedelta
from pathlib import Path

import requests
import urllib3
import cv2
import numpy as np
from PIL import Image
import pytesseract
import PyPDF2
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# UTF-8 для Windows
if sys.stdout.encoding != 'utf-8':
    if sys.platform == 'win32':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# PyMuPDF (опционально)
try:
    import fitz
    HAVE_PYMUPDF = True
except Exception:
    fitz = None
    HAVE_PYMUPDF = False

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Глобальная переменная для pdf2image
convert_from_path = None


class Config:
    """Конфигурация приложения"""
    
    TESSERACT_PATHS = [
        r'C:\Program Files\Tesseract-OCR\tesseract.exe',
        r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
        '/usr/bin/tesseract',
        '/usr/local/bin/tesseract',
        '/opt/homebrew/bin/tesseract'
    ]
    
    PDF_DPI = 150
    MIN_TEXT_LENGTH = 100
    
    GIGACHAT_AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    GIGACHAT_API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    TOKEN_REFRESH_BUFFER_MINUTES = 5


def check_dependencies():
    """Проверка системных зависимостей"""
    print("🔍 Проверка зависимостей...")
    
    # Tesseract
    tesseract_found = False
    for path in Config.TESSERACT_PATHS:
        if os.path.exists(path):
            pytesseract.pytesseract.tesseract_cmd = path
            tesseract_found = True
            print(f"  ✅ Tesseract: {path}")
            break
    
    if not tesseract_found and shutil.which('tesseract'):
        tesseract_found = True
        print("  ✅ Tesseract: в PATH")
    
    if not tesseract_found:
        print("  ❌ Tesseract OCR не найден!")
        return False
    
    # Poppler для Windows
    if sys.platform == 'win32':
        poppler_paths = [
            r'C:\Program Files\poppler\Library\bin',
            r'C:\poppler\Library\bin',
            r'C:\Program Files\poppler\bin',
        ]
        for path in poppler_paths:
            if os.path.exists(path):
                os.environ['PATH'] += os.pathsep + path
                print(f"  ✅ Poppler: {path}")
                break
    
    # pdf2image
    global convert_from_path
    if convert_from_path is None:
        try:
            from pdf2image import convert_from_path
            print("  ✅ pdf2image: OK")
        except ImportError:
            if HAVE_PYMUPDF:
                print("  ⚠️ pdf2image не установлен, используется PyMuPDF")
            else:
                print("  ❌ pdf2image не найден! Установите Poppler или PyMuPDF")
                return False
    
    # Языки Tesseract
    try:
        langs = pytesseract.get_languages()
        if 'rus' not in langs:
            print("  ❌ Tesseract: русский язык не установлен!")
            return False
        print("  ✅ Tesseract языки: OK")
    except Exception as e:
        print(f"  ❌ Tesseract языки: {e}")
        return False
    
    # API ключ
    if not os.getenv('GIGACHAT_AUTH_KEY'):
        print("  ❌ GIGACHAT_AUTH_KEY не найден в .env!")
        return False
    
    print("  ✅ API ключ найден")
    return True


class GigaChatAuth:
    """Управление токеном GigaChat"""
    
    def __init__(self):
        self._token = None
        self._token_expires_at = None
        self._auth_key = os.getenv('GIGACHAT_AUTH_KEY')
        
        if not self._auth_key:
            raise ValueError("GIGACHAT_AUTH_KEY не установлен")
    
    def _fetch_token(self):
        """Получение нового токена"""
        print(f"🔐 Обновление токена... ({datetime.now().strftime('%H:%M:%S')})")
        
        response = requests.post(
            Config.GIGACHAT_AUTH_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": f"Basic {self._auth_key}"
            },
            data={"scope": os.getenv('GIGACHAT_SCOPE', 'GIGACHAT_API_PERS')},
            verify=False,
            timeout=30
        )
        
        if response.status_code != 200:
            raise Exception(f"Ошибка авторизации: {response.status_code} - {response.text}")
        
        data = response.json()
        self._token = data["access_token"]
        
        if "expires_at" in data:
            self._token_expires_at = datetime.fromtimestamp(data["expires_at"] / 1000)
        else:
            self._token_expires_at = datetime.now() + timedelta(minutes=30)
        
        print(f"✅ Токен действителен до {self._token_expires_at.strftime('%H:%M:%S')}")
    
    @property
    def token(self):
        """Получить актуальный токен"""
        buffer = timedelta(minutes=Config.TOKEN_REFRESH_BUFFER_MINUTES)
        
        if not self._token or not self._token_expires_at:
            self._fetch_token()
        elif datetime.now() >= (self._token_expires_at - buffer):
            self._fetch_token()
        
        return self._token


class TextCleaner:
    """Очистка текста из PDF"""
    
    @staticmethod
    def clean_pdf_text(text: str) -> str:
        """Очистить и нормализовать текст"""
        # Убираем пробелы в словах на кириллице
        text = re.sub(r'([а-яА-ЯЁё])\s+([а-яА-ЯЁё])', r'\1\2', text)
        text = re.sub(r'\s+([,.;:])', r'\1', text)
        
        # Исправляем типичные ошибки OCR
        text = re.sub(r'(?<![0-9])о т(?![0-9])', 'от', text)
        text = re.sub(r'р уб', 'руб', text)
        text = re.sub(r'э лектр', 'электр', text)
        text = re.sub(r'э нерг', 'энерг', text)
        text = re.sub(r'сч ё т', 'счёт', text)
        text = re.sub(r'о снаб', 'оснаб', text)
        
        # Множественные пробелы
        text = re.sub(r'  +', ' ', text)
        
        return text.strip()


class PDFProcessor:
    
    @staticmethod
    def preprocess_image(image: Image.Image) -> Image.Image:
        """Предобработка изображения для OCR"""
        img = np.array(image)
        
        # Grayscale
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        else:
            gray = img
        
        # Увеличение контраста
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        gray = cv2.convertScaleAbs(gray, alpha=1.8, beta=10)
        
        # Адаптивная бинаризация
        binary = cv2.adaptiveThreshold(
            gray, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 15, 3
        )
        
        # Удаление шума
        denoised = cv2.fastNlMeansDenoising(binary, None, 10, 7, 21)
        
        # Морфологические операции
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        denoised = cv2.morphologyEx(denoised, cv2.MORPH_CLOSE, kernel)
        
        return Image.fromarray(denoised)
    
    @staticmethod
    def extract_text(pdf_path: str) -> str:
        """Извлечение текста из PDF"""
        pdf_path = Path(pdf_path)
        
        if not pdf_path.exists():
            raise FileNotFoundError(f"Файл не найден: {pdf_path}")
        
        print(f"📄 Обработка: {pdf_path.name}")
        text = ""
        
        # Извлечение встроенного текста
        print("  📖 Извлечение встроенного текста...")
        try:
            with open(str(pdf_path), 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                for i, page in enumerate(reader.pages):
                    page_text = page.extract_text()
                    if page_text and page_text.strip():
                        print(f"    ✅ Страница {i+1}: встроенный текст найден")
                        text += page_text + "\n"
                    else:
                        print(f"    ⚠️ Страница {i+1}: текст не найден")
        except Exception as e:
            print(f"  ⚠️ Ошибка PyPDF2: {e}")
        
        # OCR если текста нет
        if not text.strip():
            print("  📷 Используется OCR...")
            
            if HAVE_PYMUPDF and fitz is not None:
                # PyMuPDF (быстрее)
                try:
                    doc = fitz.open(str(pdf_path))
                    for i, page in enumerate(doc):
                        print(f"    Страница {i+1}/{len(doc)} (PyMuPDF)...")
                        mat = fitz.Matrix(Config.PDF_DPI / 72, Config.PDF_DPI / 72)
                        pix = page.get_pixmap(matrix=mat)
                        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                        
                        processed = PDFProcessor.preprocess_image(img)
                        page_text = pytesseract.image_to_string(
                            processed,
                            lang='rus+eng',
                            config='--oem 3 --psm 6'
                        )
                        text += page_text + "\n"
                    doc.close()
                except Exception as e:
                    print(f"    ⚠️ PyMuPDF ошибка: {e}")
            else:
                # pdf2image fallback
                images = convert_from_path(str(pdf_path), dpi=Config.PDF_DPI, fmt='png')
                for i, image in enumerate(images):
                    print(f"    Страница {i+1}/{len(images)}...")
                    processed = PDFProcessor.preprocess_image(image)
                    page_text = pytesseract.image_to_string(
                        processed,
                        lang='rus+eng',
                        config='--oem 3 --psm 6'
                    )
                    text += page_text + "\n"
        
        if not text.strip():
            raise ValueError("Не удалось извлечь текст")
        
        print(f"  ✅ Извлечено {len(text)} символов")
        return text


class InvoiceAuditor:
    """Аудитор счетов-фактур"""
    
    # УЛУЧШЕННЫЙ ПРОМПТ с явным требованием валидного JSON
    PROMPT_TEMPLATE = """Ты эксперт по аудиту счетов-фактур. Извлеки данные из текста счет-фактуры.

ВАЖНО: Верни ТОЛЬКО валидный JSON без дополнительного текста, комментариев, markdown или пояснений!

ФОРМАТ ОТВЕТА:
{{
  "invoice_number": "строка",
  "date": "YYYY-MM-DD",
  "supplier": "название организации",
  "buyer": "название организации",
  "amount": число,
  "vat": число,
  "vat_rate": число,
  "contract_number": "строка или null",
  "payment_date": "YYYY-MM-DD или null",
  "meter_number": "строка или null"
}}

ПРАВИЛА:
- invoice_number - номер счет-фактуры
- date - дата выставления счет-фактуры (формат YYYY-MM-DD)
- supplier - организация, выставившая счет
- buyer - организация, получившая счет
- amount - сумма БЕЗ НДС (число без кавычек)
- vat - сумма НДС (число без кавычек)
- vat_rate - ставка НДС в процентах (число без кавычек, например 20)
- Если данные не найдены, используй null
- НЕ добавляй пояснения, комментарии или дополнительный текст

ТЕКСТ СЧЕТ-ФАКТУРЫ:
{text}

JSON:"""
    
    def __init__(self):
        self.auth = GigaChatAuth()
        self.pdf_processor = PDFProcessor()
    
    @staticmethod
    def extract_json_from_text(text: str) -> str:
        """Извлечь JSON из текста с markdown или комментариями"""
        # Убираем markdown блоки ```json ... ```
        text = re.sub(r'```(?:json)?\s*', '', text)
        text = text.strip()
        
        # Ищем JSON объект
        start_idx = text.find('{')
        end_idx = text.rfind('}')
        
        if start_idx >= 0 and end_idx > start_idx:
            json_str = text[start_idx:end_idx + 1]
            
            # Убираем переносы строк внутри строковых значений
            # Заменяем \n на пробел внутри значений
            json_str = re.sub(r'"\s*:\s*"([^"]*)\n([^"]*)"', r'": "\1 \2"', json_str)
            
            # Убираем переносы между ключами
            json_str = re.sub(r',\s*\n\s*', ', ', json_str)
            json_str = re.sub(r'{\s*\n\s*', '{ ', json_str)
            json_str = re.sub(r'\s*\n\s*}', ' }', json_str)
            
            # Множественные пробелы
            json_str = re.sub(r'\s+', ' ', json_str)
            
            return json_str
        
        return text
    
    @staticmethod
    def parse_json_robust(text: str) -> dict:
        """Усиленный парсер JSON с множественными fallback"""
        # Шаг 1: Извлечь чистый JSON
        json_str = InvoiceAuditor.extract_json_from_text(text)
        
        # Шаг 2: Попытка прямого парсинга
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            print(f"  ⚠️ JSON decode error: {e}")
        
        # Шаг 3: Исправление типичных ошибок
        try:
            # Убираем trailing commas
            json_str = re.sub(r',\s*}', '}', json_str)
            json_str = re.sub(r',\s*\]', ']', json_str)
            
            # Исправляем одинарные кавычки
            json_str = json_str.replace("'", '"')
            
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass
        
        # Шаг 4: Ручной парсинг через regex
        result = {
            "invoice_number": None,
            "date": None,
            "supplier": None,
            "buyer": None,
            "amount": None,
            "vat": None,
            "vat_rate": None,
            "contract_number": None,
            "payment_date": None,
            "meter_number": None,
        }
        
        patterns = {
            "invoice_number": r'"invoice_number"\s*:\s*"([^"]*)"',
            "date": r'"date"\s*:\s*"([^"]*)"',
            "supplier": r'"supplier"\s*:\s*"([^"]*)"',
            "buyer": r'"buyer"\s*:\s*"([^"]*)"',
            "amount": r'"amount"\s*:\s*([0-9.]+)',
            "vat": r'"vat"\s*:\s*([0-9.]+)',
            "vat_rate": r'"vat_rate"\s*:\s*([0-9.]+)',
            "contract_number": r'"contract_number"\s*:\s*(?:"([^"]*)"|null)',
            "payment_date": r'"payment_date"\s*:\s*(?:"([^"]*)"|null)',
            "meter_number": r'"meter_number"\s*:\s*(?:"([^"]*)"|null)',
        }
        
        for key, pattern in patterns.items():
            match = re.search(pattern, json_str)
            if match:
                value = match.group(1) if match.group(1) else None
                if key in ['amount', 'vat', 'vat_rate'] and value:
                    try:
                        result[key] = float(value)
                    except ValueError:
                        result[key] = None
                else:
                    result[key] = value
        
        return result
    
    @staticmethod
    def validate_result(data: dict) -> dict:
        """Валидация результатов"""
        required_fields = ['invoice_number', 'date', 'supplier', 'buyer', 'amount', 'vat']
        for field in required_fields:
            if field not in data or data[field] is None:
                print(f"  ⚠️ Отсутствует поле: {field}")
        
        # Проверка НДС
        amount = data.get('amount', 0)
        vat = data.get('vat', 0)
        vat_rate = data.get('vat_rate', 0)
        
        if amount and vat and vat_rate:
            expected_vat = round(amount * vat_rate / 100, 2)
            if abs(vat - expected_vat) > 0.01:
                print(f"  ⚠️ Несоответствие НДС: {vat} != {expected_vat}")
        
        return data
    
    def audit(self, pdf_path: str) -> dict:
        """Аудит счет-фактуры"""
        # Извлекаем текст
        text = self.pdf_processor.extract_text(pdf_path)
        
        # Очистка
        print("🧹 Очистка текста...")
        text = TextCleaner.clean_pdf_text(text)
        
        # Аудит через GigaChat
        print("🤖 Аудит с помощью GigaChat...")
        
        prompt = self.PROMPT_TEMPLATE.format(text=text[:4000])
        
        for attempt in range(3):
            try:
                response = requests.post(
                    Config.GIGACHAT_API_URL,
                    headers={
                        "Authorization": f"Bearer {self.auth.token}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "GigaChat",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 1024
                    },
                    verify=False,
                    timeout=60
                )
                
                if response.status_code == 401:
                    print("  ⚠️ Токен истёк, обновляю...")
                    self.auth._token = None
                    continue
                
                response.raise_for_status()
                
                result_text = response.json()["choices"][0]["message"]["content"]
                
                print(f"  📝 Ответ получен ({len(result_text)} символов)")
                
                # Парсим с улучшенной обработкой
                try:
                    result = self.parse_json_robust(result_text)
                    
                    # Проверяем что это не пустой результат
                    if not result or all(v is None for v in result.values()):
                        raise ValueError("Пустой результат")
                    
                    result = self.validate_result(result)
                    return result
                    
                except Exception as e:
                    print(f"  ⚠️ Ошибка парсинга (попытка {attempt + 1}/3): {e}")
                    if attempt == 2:
                        print(f"     Сырой ответ: {result_text[:500]}")
                        return {
                            "error": "JSON parse failed",
                            "raw_response": result_text,
                            "extracted_text": text[:1000]
                        }
                    time.sleep(1)
            
            except requests.exceptions.Timeout:
                print(f"  ⏳ Таймаут ({attempt + 1}/3)")
                time.sleep(2)
            except Exception as e:
                print(f"  ❌ Ошибка: {e}")
                if attempt == 2:
                    raise
                time.sleep(1)
        
        raise Exception("Не удалось получить ответ после 3 попыток")


def main():
    print("=" * 60)
    print("🔌 АУДИТОР СЧЕТОВ-ФАКТУР v1.1 (улучшенная обработка JSON)")
    print("=" * 60)
    print()
    
    # Проверка зависимостей
    if not check_dependencies():
        print("\n❌ Исправьте ошибки и запустите снова")
        sys.exit(1)
    
    print()
    
    # Файл для аудита
    pdf_file = os.getenv('PDF_FILE', 'счет-фактура.pdf')
    
    if len(sys.argv) > 1:
        pdf_file = sys.argv[1]
    
    try:
        auditor = InvoiceAuditor()
        result = auditor.audit(pdf_file)
        
        print()
        print("=" * 60)
        print("📊 РЕЗУЛЬТАТ АУДИТА")
        print("=" * 60)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        
        # Сохраняем результат
        output_file = Path(pdf_file).stem + "_audit_result.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n💾 Сохранено в: {output_file}")
    
    except FileNotFoundError as e:
        print(f"\n❌ {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
