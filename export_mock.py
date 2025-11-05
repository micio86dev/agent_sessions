#!/usr/bin/env python3
"""
Esporta mock JSON degli eventi raw_events da MongoDB Atlas.
Usa le stesse variabili d'ambiente dello script agent_sessions.py
"""

import os, json
from datetime import datetime, timezone
from pymongo import MongoClient
from dotenv import load_dotenv

# carica il .env
load_dotenv()


def get_mongo_client():
    uri = os.environ.get("MONGO_URI")
    if not uri:
        print("MONGO_URI non impostato, esco.")
        return None
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.server_info()  # test rapido
        return client
    except Exception as e:
        print("Errore connessione Mongo:", e)
        return None


client = get_mongo_client()
if not client:
    exit(1)

db_name = os.environ.get("MONGO_DB", "agent_sessions")
db = client[db_name]
coll = db["raw_events"]

# Pipeline per raggruppare gli eventi per titolo + device_id
pipeline = [
    {
        "$match": {
            "type": {"$in": ["focus_start", "focus_end"]},
            "payload.title": {"$exists": True, "$ne": ""},
        }
    },
    {"$sort": {"payload.title": 1, "ts": 1}},
    {
        "$group": {
            "_id": {"title": "$payload.title", "device_id": "$device_id"},
            "events": {"$push": {"type": "$type", "ts": "$ts"}},
        }
    },
]

results = list(coll.aggregate(pipeline, allowDiskUse=True))

# Costruisci il JSON per il mock
mock_export = [
    {
        "title": doc["_id"]["title"],
        "device_id": doc["_id"]["device_id"],
        "events": doc["events"],
    }
    for doc in results
]

# Salva in file JSON
out_file = "mock_events.json"
with open(out_file, "w") as f:
    json.dump(mock_export, f, indent=2)

print(f"Esportati {len(mock_export)} titoli con eventi per mock nel file {out_file}.")
