import os
import re
import cv2
import numpy as np
from PIL import Image
from google import genai
from google.genai import types

def test_gemini_bbox():
    client = genai.Client() # Assumes GOOGLE_API_KEY is in env
    
    # Create a dummy image (e.g. a red square in the middle of a blank image)
    img_np = np.zeros((480, 640, 3), dtype=np.uint8)
    img_np[200:280, 300:340] = [0, 0, 255] # Red bounding box area
    
    # Convert properly
    image = Image.fromarray(cv2.cvtColor(img_np, cv2.COLOR_BGR2RGB))
    
    # The prompt for bounding box
    target_query = "red rectangle"
    prompt = f"Return a bounding box for the {target_query} in the image. Output only the bounding box in the format [ymin, xmin, ymax, xmax] where values are from 0 to 1000."
    
    print(f"Testing model with prompt: {prompt}")
    
    try:
        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=[image, prompt],
        )
        print(f"Raw response: {response.text}")
        
        match = re.search(r'\[\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\s*\]', response.text)
        if match:
            ymin, xmin, ymax, xmax = map(int, match.groups())
            print(f"Matched bounding box (normalized): ymin={ymin}, xmin={xmin}, ymax={ymax}, xmax={xmax}")
            
            # Map back to 640x480
            w, h = 640, 480
            x1 = int(xmin * w / 1000.0)
            y1 = int(ymin * h / 1000.0)
            x2 = int(xmax * w / 1000.0)
            y2 = int(ymax * h / 1000.0)
            
            print(f"Mapped to pixel coords: x1={x1}, y1={y1}, x2={x2}, y2={y2}")
            print(f"Actual red rect is: x1=300, y1=200, x2=340, y2=280")
            print("Success")
        else:
            print("Regex did not match.")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    test_gemini_bbox()
