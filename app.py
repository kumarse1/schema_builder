import streamlit as st
import pytesseract
from pytesseract import Output
from PIL import Image
import cv2
import numpy as np
import json
import hashlib
import requests
import io
import os
from typing import List, Dict, Any
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

st.title("🧾 Form Schema Extractor (PyTesseract + Vision LLM API)")

# --- Environment-based API settings ---
@st.cache_data
def load_api_config():
    """Load API configuration from environment variables"""
    return {
        "api_url": os.getenv("VISION_LLM_API_URL", "http://localhost:8000/api/vision"),
        "auth_token": os.getenv("VISION_LLM_AUTH_TOKEN", ""),
        "api_key": os.getenv("VISION_LLM_API_KEY", ""),
        "timeout": int(os.getenv("API_TIMEOUT", "30")),
        "max_retries": int(os.getenv("API_MAX_RETRIES", "3"))
    }

# Load configuration
config = load_api_config()

# Display current configuration (with masked sensitive data)
with st.expander("🔧 API Configuration", expanded=False):
    st.write(f"**API URL:** {config['api_url']}")
    st.write(f"**Auth Token:** {'***' + config['auth_token'][-4:] if config['auth_token'] else 'Not set'}")
    st.write(f"**API Key:** {'***' + config['api_key'][-4:] if config['api_key'] else 'Not set'}")
    st.write(f"**Timeout:** {config['timeout']} seconds")
    st.write(f"**Max Retries:** {config['max_retries']}")

# Option to override environment settings
override_settings = st.checkbox("🔄 Override Environment Settings")
if override_settings:
    vision_llm_api_url = st.text_input("🔧 Vision LLM API Endpoint", value=config['api_url'])
    vision_llm_auth_token = st.text_input("🔐 Bearer Token", value=config['auth_token'], type="password")
    vision_llm_api_key = st.text_input("🔑 API Key", value=config['api_key'], type="password")
else:
    vision_llm_api_url = config['api_url']
    vision_llm_auth_token = config['auth_token']
    vision_llm_api_key = config['api_key']

uploaded_file = st.file_uploader("📤 Upload a blank form image", type=["png", "jpg", "jpeg"])

def preprocess_image(image: Image.Image) -> np.ndarray:
    """Preprocess image for better OCR results"""
    img_np = np.array(image)
    gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
    
    # Apply denoising
    denoised = cv2.fastNlMeansDenoising(gray)
    
    # Apply adaptive threshold for better text detection
    thresh = cv2.adaptiveThreshold(
        denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 11, 2
    )
    
    return thresh

def extract_ocr_data(image: np.ndarray, confidence_threshold: int = 60) -> List[Dict[str, Any]]:
    """Extract OCR data with improved filtering"""
    try:
        ocr_data = pytesseract.image_to_data(image, output_type=Output.DICT)
    except Exception as e:
        st.error(f"OCR processing failed: {e}")
        return []
    
    n_boxes = len(ocr_data['text'])
    results = []
    
    for i in range(n_boxes):
        confidence = int(ocr_data['conf'][i])
        if confidence > confidence_threshold:
            text = ocr_data['text'][i].strip()
            if text and len(text) > 1:  # Filter out single characters
                x, y, w, h = (
                    ocr_data['left'][i], 
                    ocr_data['top'][i], 
                    ocr_data['width'][i], 
                    ocr_data['height'][i]
                )
                
                # Skip very small boxes (likely noise)
                if w > 5 and h > 5:
                    results.append({
                        "text": text,
                        "bbox": [x, y, x + w, y + h],
                        "confidence": confidence,
                        "line_num": ocr_data['line_num'][i],
                        "block_num": ocr_data['block_num'][i],
                        "page_num": ocr_data['page_num'][i],
                        "width": w,
                        "height": h
                    })
    
    return results

def create_annotated_image(original_image: np.ndarray, ocr_results: List[Dict[str, Any]]) -> np.ndarray:
    """Create image with bounding boxes and confidence scores"""
    annotated_image = original_image.copy()
    
    for result in ocr_results:
        x1, y1, x2, y2 = result["bbox"]
        confidence = result["confidence"]
        
        # Color based on confidence: green (high) to red (low)
        color = (0, 255, 0) if confidence > 80 else (255, 165, 0) if confidence > 70 else (255, 0, 0)
        
        cv2.rectangle(annotated_image, (x1, y1), (x2, y2), color, 2)
        
        # Add confidence score
        cv2.putText(
            annotated_image, 
            f"{confidence}%", 
            (x1, y1 - 5), 
            cv2.FONT_HERSHEY_SIMPLEX, 
            0.5, 
            color, 
            1
        )
    
    return annotated_image

def generate_vision_llm_prompt(form_meta: Dict[str, Any], ocr_results: List[Dict[str, Any]]) -> str:
    """Generate improved prompt for Vision LLM"""
    return f"""
You are a Vision LLM helping extract structured form schema from OCR data.

OCR data was extracted from a blank form template using PyTesseract. Your goal is to:
✅ Identify fields that a human would be expected to fill in
✅ Provide the field name (as labeled on the form)
✅ Determine the data type (string, number, date, email, phone, etc.)
✅ Return the exact bounding box that represents the user-input area — not the label
✅ Assign a logical section name based on headings, titles, or spatial grouping
✅ Identify field relationships (e.g., required fields, dependent fields)

Guidelines:
❌ Do NOT return bounding boxes for labels only (e.g., 'Patient Name')
❌ Do NOT include decorative text, titles, or instructions
❌ Do NOT guess values — only infer where input is expected
❌ Exclude legal disclaimers, instructions, or footer text
✅ Look for common form patterns: underlines, boxes, checkboxes, signature lines
✅ Group related fields logically (Personal Info, Address, Emergency Contact, etc.)

Form Metadata:
{json.dumps(form_meta, indent=2)}

OCR Results ({len(ocr_results)} items):
{json.dumps(ocr_results, indent=2)}

Return your output as a JSON object with this structure:
{{
  "form_schema": {{
    "form_id": "{form_meta['form_id']}",
    "sections": [
      {{
        "section_name": "string",
        "fields": [
          {{
            "field_name": "string",
            "data_type": "string|number|date|email|phone|boolean|select",
            "bounding_box": [x1, y1, x2, y2],
            "required": boolean,
            "validation_rules": "string (optional)",
            "placeholder": "string (optional)"
          }}
        ]
      }}
    ]
  }}
}}
"""

def call_vision_llm_api(
    api_url: str, 
    image_bytes: bytes, 
    prompt: str, 
    auth_token: str = None,
    api_key: str = None,
    timeout: int = 30,
    max_retries: int = 3
) -> Dict[str, Any]:
    """Call Vision LLM API with proper error handling and retry logic"""
    
    # Prepare headers
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if api_key:
        headers["X-API-Key"] = api_key
    
    # Prepare files and data
    files = {"image": ("form.jpg", image_bytes, "image/jpeg")}
    data = {"prompt": prompt}
    
    # Retry logic
    for attempt in range(max_retries):
        try:
            with st.spinner(f"Calling Vision LLM API... (Attempt {attempt + 1}/{max_retries})"):
                response = requests.post(
                    api_url, 
                    files=files, 
                    data=data, 
                    headers=headers,
                    timeout=timeout
                )
                response.raise_for_status()
                
                # Validate response content type
                content_type = response.headers.get('content-type', '')
                if 'application/json' not in content_type:
                    st.warning(f"⚠️ Unexpected content type: {content_type}")
                
                return response.json()
                
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                st.warning(f"⏱️ Request timed out on attempt {attempt + 1}. Retrying...")
                continue
            st.error("❌ Request timed out after all retry attempts.")
            return None
            
        except requests.exceptions.ConnectionError:
            if attempt < max_retries - 1:
                st.warning(f"🔌 Connection failed on attempt {attempt + 1}. Retrying...")
                continue
            st.error("❌ Could not connect to Vision LLM API after all retry attempts. Check the endpoint URL.")
            return None
            
        except requests.exceptions.HTTPError as e:
            status_code = response.status_code
            
            # Handle specific HTTP error codes
            if status_code == 401:
                st.error("❌ Authentication failed. Check your API credentials.")
            elif status_code == 403:
                st.error("❌ Access forbidden. Check your API permissions.")
            elif status_code == 429:
                if attempt < max_retries - 1:
                    st.warning(f"⏳ Rate limited on attempt {attempt + 1}. Retrying...")
                    continue
                st.error("❌ Rate limit exceeded. Please try again later.")
            elif status_code >= 500:
                if attempt < max_retries - 1:
                    st.warning(f"🔧 Server error on attempt {attempt + 1}. Retrying...")
                    continue
                st.error(f"❌ Server error {status_code}: {response.text}")
            else:
                st.error(f"❌ HTTP Error {status_code}: {response.text}")
            return None
            
        except json.JSONDecodeError:
            st.error("❌ Invalid JSON response from API")
            st.text("Response content:")
            st.code(response.text[:500])  # Show first 500 chars of response
            return None
            
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                st.warning(f"🔄 Request failed on attempt {attempt + 1}: {e}. Retrying...")
                continue
            st.error(f"❌ Request failed after all retry attempts: {e}")
            return None
    
    return None

if uploaded_file is not None:
    try:
        # Load and validate image
        image = Image.open(uploaded_file).convert("RGB")
        
        # Validate image size
        if image.width < 100 or image.height < 100:
            st.error("❌ Image too small. Please upload a larger image.")
            st.stop()
        
        if image.width * image.height > 50_000_000:  # ~50MP limit
            st.warning("⚠️ Large image detected. Resizing for better performance...")
            image.thumbnail((3000, 3000), Image.Resampling.LANCZOS)
        
        # Generate unique form ID
        img_bytes = io.BytesIO()
        image.save(img_bytes, format='JPEG', quality=95)
        form_hash = hashlib.md5(img_bytes.getvalue()).hexdigest()
        st.success(f"📄 Unique Form ID (MD5): {form_hash}")
        
        # Image preprocessing options
        st.subheader("🔧 Preprocessing Options")
        col1, col2 = st.columns(2)
        
        with col1:
            confidence_threshold = st.slider("OCR Confidence Threshold", 30, 90, 60)
        with col2:
            use_preprocessing = st.checkbox("Enhanced Preprocessing", value=True)
        
        # Preprocess image
        if use_preprocessing:
            processed_image = preprocess_image(image)
        else:
            processed_image = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
            _, processed_image = cv2.threshold(processed_image, 150, 255, cv2.THRESH_BINARY_INV)
        
        # Extract OCR data
        ocr_results = extract_ocr_data(processed_image, confidence_threshold)
        
        if not ocr_results:
            st.warning("⚠️ No high-confidence text detected. Try adjusting the confidence threshold or using a clearer image.")
        else:
            # Display annotated image
            annotated_img = create_annotated_image(np.array(image), ocr_results)
            st.image(annotated_img, caption=f"Detected {len(ocr_results)} text regions", channels="RGB")
            
            # Show OCR results
            with st.expander(f"🔍 OCR Results ({len(ocr_results)} items)", expanded=False):
                st.json(ocr_results)
            
            # Form metadata
            form_meta = {
                "form_id": form_hash,
                "image_width": image.width,
                "image_height": image.height,
                "num_ocr_entries": len(ocr_results),
                "confidence_threshold": confidence_threshold,
                "preprocessing_enabled": use_preprocessing
            }
            
            # Generate and display prompt
            prompt = generate_vision_llm_prompt(form_meta, ocr_results)
            
            with st.expander("🧠 Vision LLM Prompt", expanded=False):
                st.code(prompt, language="text")
            
            # Call Vision LLM API
            if st.button("🚀 Extract Form Schema"):
                if not vision_llm_api_url.strip():
                    st.error("❌ Please provide a valid API endpoint URL.")
                else:
                    img_bytes.seek(0)  # Reset buffer position
                    response = call_vision_llm_api(
                        vision_llm_api_url, 
                        img_bytes.getvalue(), 
                        prompt, 
                        vision_llm_auth_token if vision_llm_auth_token.strip() else None,
                        vision_llm_api_key if vision_llm_api_key.strip() else None,
                        config['timeout'],
                        config['max_retries']
                    )
                    
                    if response:
                        st.subheader("📦 Form Schema Extracted")
                        st.json(response)
                        
                        # Option to download the schema
                        schema_json = json.dumps(response, indent=2)
                        st.download_button(
                            label="💾 Download Form Schema",
                            data=schema_json,
                            file_name=f"form_schema_{form_hash[:8]}.json",
                            mime="application/json"
                        )
    
    except Exception as e:
        st.error(f"❌ Error processing image: {e}")
        st.exception(e)  # Show full traceback in development

# --- Sidebar with help information ---
with st.sidebar:
    st.markdown("### 📖 Help & Tips")
    st.markdown("""
    **For best results:**
    - Use high-resolution, clear images
    - Ensure good contrast between text and background
    - Avoid skewed or rotated forms
    - Test different confidence thresholds
    
    **Supported formats:** PNG, JPG, JPEG
    
    **Environment Configuration:**
    Create a `.env` file with:
    ```
    VISION_LLM_API_URL=https://your-api.com/vision
    VISION_LLM_AUTH_TOKEN=your_bearer_token
    VISION_LLM_API_KEY=your_api_key
    API_TIMEOUT=30
    API_MAX_RETRIES=3
    ```
    
    **API Requirements:**
    - Endpoint should accept multipart form data
    - Image field: 'image'
    - Prompt field: 'prompt'
    - Supports Bearer token and/or API key authentication
    """)
    
    # Environment file status
    st.markdown("### 🔍 Environment Status")
    env_file_exists = os.path.exists('.env')
    if env_file_exists:
        st.success("✅ .env file found")
    else:
        st.warning("⚠️ .env file not found")
        
    # Show which environment variables are set
    env_vars = {
        "VISION_LLM_API_URL": bool(os.getenv("VISION_LLM_API_URL")),
        "VISION_LLM_AUTH_TOKEN": bool(os.getenv("VISION_LLM_AUTH_TOKEN")),
        "VISION_LLM_API_KEY": bool(os.getenv("VISION_LLM_API_KEY")),
        "API_TIMEOUT": bool(os.getenv("API_TIMEOUT")),
        "API_MAX_RETRIES": bool(os.getenv("API_MAX_RETRIES"))
    }
    
    for var, is_set in env_vars.items():
        status = "✅" if is_set else "❌"
        st.text(f"{status} {var}")
        
    if st.button("🔄 Reload Environment"):
        st.cache_data.clear()
        st.rerun()
