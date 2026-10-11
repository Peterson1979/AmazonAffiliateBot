# AmazonAffiliateBot

A modular, standalone Python automation tool that scrapes Amazon affiliate product pages using Selenium and Firefox, extracts and normalizes product data, and uses a local **Ollama** LLM (`qwen2.5:7b`) to generate platform-specific, strictly grounded social media copy for **X, Facebook, Pinterest, and Threads**.

---

## Architecture (V2)

```text
Amazon Affiliate URL
        │
        ▼
[Selenium + Firefox] (Headless or GUI)
        │
        ▼
[Normalized Product Dataclass] (Primitive Python strings / lists)
        │
        ├─────────────────────────┐
        ▼                         ▼
[Local Product Cache]     [Platform-Specific Ollama Prompts (qwen2.5:7b)]
 (data/products/*.json)           │
                                  ├─ X (Short, natural, concise product facts)
                                  ├─ Facebook (2-4 paragraphs, readable breakdown)
                                  ├─ Pinterest (JSON title + description)
                                  └─ Threads (Casual, conversational)
                                  │
                                  ▼
                        [Claim Grounding Validator]
                                  │ (Validates against Product facts)
                                  ├─ Invalid ──► Regenerate (Max 1 retry)
                                  │
                                  ▼
                     [Structured Social JSON Output]
                      (data/generated/*_social.json)
                                  │
                                  ▼
                        [Terminal Previews]
                                  │
                                  ▼
                    [Manual Review (NO Auto-Publish)]
```

> **Note**: V2 does **NOT** publish to social media automatically. It generates structured, compliant copy for manual review and staging. Publishing adapters can be added in a future phase without modifying the scraper or content generator.

---

## V2 Content Grounding & Anti-Hallucination Rule

The generated social content is based **ONLY** on factual information contained in the normalized Product data.

* **Allowed**: Rephrase facts, shorten facts, combine facts, adapt wording for platform style, organize existing facts.
* **Strictly Forbidden**:
  - Invented facts, numbers, or dimensions.
  - Unsupported benefits or use-cases (e.g. no "cures back pain", "great for gaming" unless in source data).
  - Invented customer problems, struggles, or pain points.
  - Prices, discounts, ratings, reviews, or shipping claims.
  - Fake personal experience or customer satisfaction claims.
  - Generic marketing hype ("game changer", "must-have", "improves your life").

---

## Prerequisites

1. **Python**: Python 3.10+ (tested on Python 3.14 on Windows).
2. **Mozilla Firefox**: Standard Firefox browser installed.
3. **Ollama**: Local Ollama server running on `http://localhost:11434`.
4. **LLM Model**: `qwen2.5:7b` installed in Ollama.

To verify / pull the required model:
```powershell
ollama pull qwen2.5:7b
ollama list
```

---

## Installation

1. Navigate to the project directory:
   ```powershell
   cd C:\Users\opeti\AmazonAffiliateBot
   ```

2. Install the required dependencies:
   ```powershell
   pip install -r requirements.txt
   ```

---

## Configuration (`config.json`)

Configuration is stored in `config.json`:

```json
{
  "ollama": {
    "base_url": "http://localhost:11434",
    "model": "qwen2.5:7b",
    "timeout": 300,
    "num_predict": 120,
    "num_ctx": 1024
  },
  "affiliate": {
    "disclosure": "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."
  },
  "browser": {
    "headless": false,
    "firefox_profile": ""
  }
}
```

- **`ollama.base_url` & `ollama.model`**: Ollama endpoint and local model name.
- **`affiliate.disclosure`**: Configured affiliate disclosure appended across all platforms.
- **`browser.headless`**: Set to `false` during development to observe browser scraping live, or `true` for headless execution.

---

## Usage

### 1. Generate Social Media Content (All 4 Platforms)
Run the main runner with any Amazon affiliate or product URL:

```powershell
python amazon_affiliate.py "https://link.amazon/B0hwa4Nkm"
```

If the product was already scraped, it instantly loads from the local cache (`data/products/`) to avoid redundant Amazon requests.

### 2. Generate for a Specific Platform
```powershell
python amazon_affiliate.py "https://link.amazon/B0hwa4Nkm" --platform x
python amazon_affiliate.py "https://link.amazon/B0hwa4Nkm" --platform facebook
python amazon_affiliate.py "https://link.amazon/B0hwa4Nkm" --platform pinterest
python amazon_affiliate.py "https://link.amazon/B0hwa4Nkm" --platform threads
```

### 3. Additional CLI Flags
* `--force-scrape`: Bypass the local product cache and scrape fresh data from Amazon.
* `--headless`: Run Firefox in headless mode.
* `--v1`: Generate a single generic recommendation (legacy V1 mode).
* `--config <path>`: Specify an alternate config JSON path.

---

## Data Storage

* **Product Cache**: Saved as structured JSON files under `data/products/` (e.g. `asin_B0GKGBWRTR.json`).
* **Generated Social Content**: Saved as structured JSON files under `data/generated/` (e.g. `asin_B0GKGBWRTR_social.json`).

### Generated JSON Structure:
```json
{
  "asin": "B0GKGBWRTR",
  "product_title": "Laptop Stand Ergonomic Adjustable...",
  "generated_with": "qwen2.5:7b",
  "platforms": {
    "x": {
      "text": "..."
    },
    "facebook": {
      "text": "..."
    },
    "pinterest": {
      "title": "...",
      "description": "..."
    },
    "threads": {
      "text": "..."
    }
  }
}
```

---

## Running Tests

Run the test suite (zero external test runner dependencies):
```powershell
python tests/run_tests.py
```

Or via standard unittest discovery:
```powershell
python -m unittest discover -s tests
```

---

## Project Structure

```text
AmazonAffiliateBot/
├── amazon/
│   ├── __init__.py
│   └── scraper.py         # Selenium Firefox scraper and Product dataclass
├── ai/
│   ├── __init__.py
│   └── ollama.py          # Local Ollama client (qwen2.5:7b)
├── content/
│   ├── __init__.py
│   ├── prompts.py         # Dedicated prompts for X, FB, Pinterest, Threads
│   ├── validator.py       # Factual grounding & claim validator
│   └── generator.py       # Multi-platform content generator & 1-retry orchestration
├── data/
│   ├── products/          # Local JSON product cache
│   └── generated/         # Stored AI social media outputs
├── tests/
│   ├── run_tests.py       # Unit test runner
│   ├── test_prompts.py    # Prompt template tests
│   ├── test_generator.py  # Validator, negative tests & mock tests
│   └── test_cache.py      # Cache loading tests
├── amazon_affiliate.py    # Main CLI runner with multi-platform previews
├── config.json            # Configuration settings
├── requirements.txt       # Python dependencies
└── README.md              # Project documentation
```
