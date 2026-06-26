import os
import requests
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("FOOTBALL_DATA_KEY")
print(f"FOOTBALL_DATA_KEY: {key}")

url = "https://api.football-data.org/v4/competitions/WC/matches"
headers = {"X-Auth-Token": key}

response = requests.get(url, headers=headers, timeout=15)
print(f"Status: {response.status_code}")
print("Response:")
print(response.text)
