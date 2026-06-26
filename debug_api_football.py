import requests
import os
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("API_FOOTBALL_KEY")
print(f"Key encontrada: {key[:10]}..." if key else "ERROR: Key no encontrada en .env")

url = "https://v3.football.api-sports.io/fixtures"
headers = {"x-apisports-key": key}
params = {"league": 1, "season": 2026, "date": "2026-06-23"}

response = requests.get(url, headers=headers, params=params)
print(f"Status: {response.status_code}")
print(f"Response: {response.text[:500]}")
