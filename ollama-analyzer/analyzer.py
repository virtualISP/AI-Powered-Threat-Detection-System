from elasticsearch import Elasticsearch
import ollama
import time
import json
import os
from datetime import datetime

ELASTICSEARCH_HOST = os.getenv("ELASTICSEARCH_HOST", "http://elasticsearch:9200")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")

MODEL = "phi3:mini"  
AI_THREATS_INDEX = "ai-threats"
POLL_INTERVAL = 5

AI_THREATS_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0
    },
    "mappings": {
        "properties": {
            "@timestamp": {"type": "date"},
            "log": {"type": "text"},
            "source": {"type": "keyword"},
            "threat": {"type": "keyword"}, 
            "confidence": {"type": "integer"},
            "evidence": {"type": "object"},
            "recommendation": {"type": "text"}
            }
        }
    }

def create_index_if_not_exists(es_client, index_name, mapping):
    """Ensures the target index exists with proper mapping."""
    try:
        if not es_client.options(ignore_status=400).indices.exists(index=index_name):
            es_client.options(ignore_status=400).indices.create(
                index=index_name,
                body=mapping,
            )
            print(f"Created index '{index_name}' with mapping")
        else:
            es_client.options(ignore_status=400).indices.put_mapping(
                index=index_name,
                body=mapping["mappings"],
            )
    except Exception as e:
        print(f"Index setup error: {str(e)}")
        raise

ollama_client = ollama.Client(host=OLLAMA_HOST)

def analyze_log(log_entry):
    """Analyzes log entry using Ollama LLM."""
    prompt = f"""Analyze this log for security threats. Return a SINGLE JSON object with:
    - threat: "none/malware/phishing/brute_force/sqli/xss"
    - confidence: 0-100
    - evidence: technical details
    - recommendation: action items
    
    Log: {log_entry}"""
    
    try:
        response = ollama_client.generate(
            model=MODEL,
            prompt=prompt,
            options={"temperature": 0.1}
        )
        
        raw = response["response"].strip()
        if raw.startswith('```json'):
            raw = raw[7:]
        if raw.endswith('```'):
            raw = raw[:-3]
            
        if '}\n{' in raw:
            parts = raw.split('}\n{')
            merged = '{' + parts[0].split('{', 1)[-1] + ',' + parts[1].rsplit('}', 1)[0] + '}'
            return json.loads(merged)
            
        return json.loads(raw)
    except Exception as e:
        print(f"Analysis failed. Raw response: {raw if 'raw' in locals() else 'N/A'}")
        print(f"Error: {str(e)}")
        return None

def process_log(es_client, log_entry, timestamp):
    """Handles the full processing pipeline for a single log entry."""
    analysis = analyze_log(log_entry)
    if not analysis:
        return False
    
    document = {
        "@timestamp": timestamp,
        "log": log_entry,
        "source": "nginx",
        **analysis 
    }
    
    try:
        response = es_client.index(
            index=AI_THREATS_INDEX,
            document=document,
            refresh=True
        )
        print(f"✅ Successfully indexed document ID: {response['_id']}")
        return True
    except Exception as e:
        print(f"❌ Indexing failed: {str(e)}")
        return False


def main():
    print(f"Starting AI Threat Analyzer (Model: {MODEL})")
    
    es = None
    for attempt in range(10):
        try:
            es = Elasticsearch(
                ELASTICSEARCH_HOST,
                request_timeout=60,
                max_retries=3,
                retry_on_timeout=True
            )
            if es.ping():
                print("Connected to Elasticsearch.")
                break
        except Exception as e:
            print(f"Attempt {attempt+1}/10 - Elasticsearch not ready: {e}")
            time.sleep(5)
    else:
        print("Failed to connect to Elasticsearch after 10 attempts. Exiting.")
        exit(1)

    create_index_if_not_exists(es, AI_THREATS_INDEX, AI_THREATS_MAPPING)

    while True:
        try:

            print("Searching for latest log...")
            res = es.search(
            index="logs-*",
            body={
                "size": 1,
                "query": {
                    "bool": {
                        "must_not": {
                            "exists": {
                                "field": "processed_by_analyzer"
                            }
                        }
                    }
                },
                "sort": [{"@timestamp": {"order": "desc"}}],
                "_source": True
            }
            )

            if res["hits"]["hits"]:
                latest_log = res["hits"]["hits"][0]
                print("Latest log document:", json.dumps(latest_log["_source"], indent=2))

                log_msg = latest_log["_source"].get("message") or latest_log["_source"].get("log", {}).get("message")
                if log_msg:
                    if process_log(es, log_msg, latest_log["_source"]["@timestamp"]):
                        print(f"Processed log: {log_msg[:50]}...")
                else:
                    print("No usable log message found in latest log")

            time.sleep(POLL_INTERVAL)
            
        except KeyboardInterrupt:
            print("Shutting down...")
            break
        except Exception as e:
            print(f"Critical error: {str(e)}")
            time.sleep(10) 

if __name__ == "__main__":
    main()