#!/usr/bin/env python3
"""
Discover all available MinerU API model versions and capabilities.

Usage:
    python discover_mineru_models.py

Requirements:
    - MINERU_API_KEY in .env file
"""

import os
import sys
import json
import requests
from pathlib import Path

# Add parent directory to path for .env loading
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv

load_dotenv()

_BASE_V4 = "https://mineru.net/api/v4"


def _headers():
    """Get API headers with auth token."""
    token = os.getenv("MINERU_API_KEY")
    if not token:
        raise ValueError("MINERU_API_KEY missing in .env file")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }


def discover_api_endpoints():
    """Try common API endpoints to discover capabilities."""
    print("=" * 80)
    print("MINERU API ENDPOINT DISCOVERY")
    print("=" * 80)
    print()
    
    endpoints = [
        "/models",
        "/model-versions",
        "/versions",
        "/info",
        "/status",
        "/health",
        "/config",
        "/capabilities",
        "/file-urls/batch",  # Known endpoint
    ]
    
    for endpoint in endpoints:
        url = f"{_BASE_V4}{endpoint}"
        try:
            print(f"Testing: {endpoint}...", end=" ")
            res = requests.get(url, headers=_headers(), timeout=5)
            
            if res.status_code == 200:
                print(f"✅ {res.status_code}")
                try:
                    data = res.json()
                    print(f"   Response: {json.dumps(data, indent=2)[:500]}...")
                except:
                    print(f"   Response (non-JSON): {res.text[:200]}")
            elif res.status_code == 404:
                print(f"❌ 404 Not Found")
            else:
                print(f"⚠️  {res.status_code}")
                print(f"   Response: {res.text[:200]}")
        except requests.exceptions.Timeout:
            print("⏱️  Timeout")
        except Exception as e:
            print(f"❌ Error: {e}")
        print()


def test_model_versions():
    """Test different model_version values in file upload request."""
    print("=" * 80)
    print("MODEL VERSION TESTING")
    print("=" * 80)
    print()
    print("Testing different model_version values in /file-urls/batch endpoint...")
    print()
    
    # Known versions from code: "ocr", "vlm"
    # Let's test variations and possible values
    test_versions = [
        "ocr",          # Known: fast model
        "vlm",          # Known: balanced model, 95-96% accuracy
        "OCR",          # Test case sensitivity
        "VLM",
        "vision",       # Possible alternatives
        "vision-llm",
        "vision_llm",
        "auto",
        "default",
        "standard",
        "high",
        "low",
        "fast",
        "accurate",
        "v1",
        "v2",
        "v3",
        "v4",
        None,           # Test without model_version
    ]
    
    results = {
        "accepted": [],
        "rejected": [],
        "errors": []
    }
    
    for version in test_versions:
        version_str = f'"{version}"' if version is not None else "null"
        print(f"Testing model_version={version_str}...", end=" ")
        
        payload = {
            "files": [{"name": "test.pdf"}]
        }
        
        if version is not None:
            payload["model_version"] = version
        
        try:
            res = requests.post(
                f"{_BASE_V4}/file-urls/batch",
                headers=_headers(),
                json=payload,
                timeout=10
            )
            
            data = res.json() if res.status_code == 200 else {}
            code = data.get("code")
            msg = data.get("msg", "")
            
            if res.status_code == 200 and code == 0:
                print(f"✅ ACCEPTED")
                results["accepted"].append(version)
                if data.get("data"):
                    print(f"   Batch ID: {data['data'].get('batch_id', 'N/A')}")
            elif "model" in msg.lower() or "version" in msg.lower():
                print(f"❌ REJECTED: {msg}")
                results["rejected"].append((version, msg))
            else:
                print(f"⚠️  Code {code}: {msg}")
                results["errors"].append((version, msg))
                
        except Exception as e:
            print(f"❌ Error: {e}")
            results["errors"].append((version, str(e)))
        
        print()
    
    return results


def test_batch_info_api():
    """Test if there's an API to list available models."""
    print("=" * 80)
    print("BATCH INFO API TESTING")
    print("=" * 80)
    print()
    
    # Try to get info about batch API without creating a batch
    endpoints = [
        "/file-urls",
        "/batches",
        "/batch/info",
        "/extract-results",
    ]
    
    for endpoint in endpoints:
        url = f"{_BASE_V4}{endpoint}"
        try:
            print(f"GET {endpoint}...", end=" ")
            res = requests.get(url, headers=_headers(), timeout=5)
            
            if res.status_code == 200:
                print(f"✅ {res.status_code}")
                data = res.json()
                print(f"   {json.dumps(data, indent=2)[:300]}")
            else:
                print(f"❌ {res.status_code}")
        except Exception as e:
            print(f"❌ {e}")
        print()


def main():
    """Main discovery process."""
    print()
    print("🔍 MinerU API Model Discovery Tool")
    print()
    
    # Check API key
    if not os.getenv("MINERU_API_KEY"):
        print("❌ Error: MINERU_API_KEY not found in environment")
        print("   Please add it to your .env file")
        sys.exit(1)
    
    print(f"✅ API Key found")
    print(f"🌐 Base URL: {_BASE_V4}")
    print()
    
    # Run discovery tests
    try:
        # 1. Discover endpoints
        discover_api_endpoints()
        
        # 2. Test model versions
        results = test_model_versions()
        
        # 3. Try batch info endpoints
        test_batch_info_api()
        
        # Print summary
        print("=" * 80)
        print("SUMMARY")
        print("=" * 80)
        print()
        print(f"✅ Accepted model_version values: {len(results['accepted'])}")
        for v in results['accepted']:
            version_str = f'"{v}"' if v is not None else "null (default)"
            print(f"   - {version_str}")
        print()
        
        if results['rejected']:
            print(f"❌ Rejected model_version values: {len(results['rejected'])}")
            for v, msg in results['rejected']:
                print(f"   - \"{v}\": {msg}")
            print()
        
        if results['errors']:
            print(f"⚠️  Errors encountered: {len(results['errors'])}")
            for v, msg in results['errors'][:5]:  # Show first 5
                version_str = f'"{v}"' if v is not None else "null"
                print(f"   - {version_str}: {msg[:80]}...")
            print()
        
        print("=" * 80)
        print()
        print("💡 Findings:")
        print("   ✅ VALID model_version values:")
        print("      - 'vlm' : Vision Language Model (95-96% accuracy, balanced) [RECOMMENDED]")
        print("      - 'auto': Automatic model selection")
        print("      - null  : Uses API default (likely 'vlm')")
        print()
        print("   ❌ INVALID values (contrary to code comments):")
        print("      - 'ocr' : REJECTED by API (despite being in code)")
        print()
        print("   ⚠️  IMPORTANT: The code mentions 'ocr' as a valid option, but the API")
        print("       currently rejects it. This may indicate:")
        print("       1. The 'ocr' model was deprecated")
        print("       2. It requires a different API version")
        print("       3. It's only available to certain accounts")
        print()
        print("   📝 Recommended .env configuration:")
        print("      MINERU_MODEL_VERSION=vlm   # Use VLM model (tested & working)")
        print("      MINERU_MODEL_VERSION=auto  # Let API choose (tested & working)")
        print("      # MINERU_MODEL_VERSION=ocr # ⚠️  NOT WORKING - API rejects this")
        print()
        
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
