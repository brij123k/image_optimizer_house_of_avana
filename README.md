# Image Optimizer — House of Avana

Local Flask tool that compresses Shopify product images in place.
Scope by entire store or by collection, with per-product selection
and optional WebP conversion.

## Setup

    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env    # then fill in your credentials
    python3 app.py

Open http://localhost:5056
