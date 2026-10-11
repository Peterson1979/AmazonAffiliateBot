import os
import sys
import json
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(r"C:\Users\opeti\AmazonAffiliateBot\.env"))

fb_token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")
fb_page_id = os.getenv("FACEBOOK_PAGE_ID", "226949230493910")
fb_photo_id = "122325680732197726"

print("--- VERIFYING FACEBOOK PHOTO POST ---")
if fb_token:
    url = f"https://graph.facebook.com/v19.0/{fb_photo_id}?fields=id,link,images,name,created_time&access_token={fb_token}"
    resp = requests.get(url, timeout=10)
    print("FB HTTP Status:", resp.status_code)
    if resp.status_code == 200:
        fb_data = resp.json()
        print("FB Photo ID:", fb_data.get("id"))
        print("FB Link:", fb_data.get("link"))
        print("FB Created Time:", fb_data.get("created_time"))
        print("FB Images Count:", len(fb_data.get("images", [])))
        if fb_data.get("images"):
            print("FB First Image Source:", fb_data["images"][0].get("source"))
    else:
        print("FB Error:", resp.text)
else:
    print("FB access token missing")

print("\n--- VERIFYING THREADS POST ---")
th_token = os.getenv("THREADS_ACCESS_TOKEN")
th_post_id = "18144904363600584"
if th_token:
    url = f"https://graph.threads.net/v1.0/{th_post_id}?fields=id,media_type,media_url,permalink,text,timestamp&access_token={th_token}"
    resp = requests.get(url, timeout=10)
    print("Threads HTTP Status:", resp.status_code)
    if resp.status_code == 200:
        th_data = resp.json()
        print("Threads Post ID:", th_data.get("id"))
        print("Threads Media Type:", th_data.get("media_type"))
        print("Threads Media URL:", th_data.get("media_url"))
        print("Threads Permalink:", th_data.get("permalink"))
        print("Threads Timestamp:", th_data.get("timestamp"))
    else:
        print("Threads Error:", resp.text)
else:
    print("Threads access token missing")

print("\n--- VERIFYING PINTEREST PIN ---")
pin_token = os.getenv("PINTEREST_ACCESS_TOKEN")
pin_id = "1033646551998850640"
if pin_token:
    url = f"https://api.pinterest.com/v5/pins/{pin_id}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {pin_token}"}, timeout=10)
    print("Pinterest HTTP Status:", resp.status_code)
    if resp.status_code == 200:
        pin_data = resp.json()
        print("Pin ID:", pin_data.get("id"))
        print("Pin Title:", pin_data.get("title"))
        print("Pin Link:", pin_data.get("link"))
        print("Pin Media:", pin_data.get("media"))
    else:
        print("Pinterest Error:", resp.text)
else:
    print("Pinterest access token missing")
