# Dynamic Web Archiver (BFS + SingleFile CLI)

Aplikasi web berbasis **Flask**, **Playwright**, dan **SingleFile CLI** untuk mengarsipkan website dinamis maupun Single Page Application (SPA) menjadi file `.html` mandiri yang dapat dinavigasi secara offline.

## Fitur Utama
- **BFS Multi-Page Crawler:** Penelusuran tautan otomatis hingga ke subdomain dan rute SPA (`routerlink`, `data-href`, serta simulasi klik tombol navigasi).
- **SingleFile Standalone HTML:** Membungkus seluruh CSS, gambar, dan font ke dalam satu file `.html` presisi tinggi tanpa dependensi eksternal.
- **Offline Link Rewriter & Modal Cleaner:** Membersihkan overlay/popup penghalang dan menulis ulang tautan antar-halaman agar terhubung secara lokal.
- **Session State Support (`auth.json`):** Mendukung penyimpanan sesi login resmi untuk mengarsipkan halaman terautentikasi.

## Cara Instalasi (Linux / WSL)
```bash
# 1. Buat virtual environment & install dependensi Python
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install chromium

# 2. Install SingleFile CLI (Node.js)
sudo npm install -g single-file-cli

# 3. Jalankan aplikasi
sudo ./venv/bin/python3 app.py

