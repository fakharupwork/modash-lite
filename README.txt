AMANA Modash Lite
=================

This package was built from:
- AMANA_Fakhar_Final_Batch (1).xlsx
- 767 keyword searches
- 36 language codes
- 59 country configurations

WHAT IT DOES
------------
1) Search Queue
   Builds one row per AMANA keyword search for a selected country/language,
   including follower range, field (Bio/Captions), theme and platform.

2) Filter Creators
   Upload a CSV/XLSX creator export, map columns, then run:
   - follower min/max
   - platform
   - creator country
   - language
   - valid-looking email
   - last post age (if available)
   - each AMANA keyword in its correct Bio or Captions field

3) Output
   - matched creators
   - keyword-by-keyword stats
   - AMANA delivery-ready CSV in the original 17-column template order

4) Dedupe
   Normalizes handles and deduplicates profiles while preserving all matched
   keyword/theme evidence.

HOW TO RUN ON WINDOWS
---------------------
Option A:
1. Double-click run_windows.bat
2. Your browser should open the app.

Option B:
1. Open Command Prompt in this folder.
2. Run:
   python -m pip install -r requirements.txt
   python -m streamlit run app.py

Mac/Linux:
   python3 -m pip install -r requirements.txt
   python3 -m streamlit run app.py

IMPORTANT LIMITATION
--------------------
This is a Modash-style filtering/search tool for creator data you already have
or export from a scraper/platform. It does not include Modash's private creator
database and does not crawl Instagram/TikTok/YouTube by itself.

BEST INPUT COLUMNS
------------------
platform, account_handle, full_name, followers, bio, captions, email, country,
language, website, profile_url, email_source_url, last_post_date,
country_evidence_url, notes

The app also auto-detects many common alternative column names.
