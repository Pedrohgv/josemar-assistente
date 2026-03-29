# AGENTS.md

**Purpose:** Guidance for AI assistants working with skills in this repository.

## Skills Overview

The Josemar Assistente uses the OpenClaw skills system to extend functionality. Skills are external tools that can be called by the AI agent to perform specific tasks.

## Two-Tier Skill System

Josemar Assistente implements a two-tier skill architecture:

### 1. Repo Skills (This Directory)
- **Location**: `/root/.openclaw/repo-skills/` (inside container)
- **Source**: `repo-skills/` directory in this repository
- **Purpose**: Version-controlled, production-ready skills
- **Deployment**: Copied to container on startup (smart deployment with skip-if-exists)
- **Persistence**: Can be modified by agent during runtime; modifications preserved on redeploy
- **Priority**: Base version - can be overridden by runtime skills

### 2. Runtime Skills (Assistant-Created)
- **Location**: `/root/.openclaw/skills/` (inside container)
- **Source**: Created by the assistant during conversations
- **Purpose**: Rapid prototyping, agent experimentation, user customization
- **Deployment**: Never touched by repo deployment
- **Persistence**: Always preserved across deployments
- **Priority**: Higher than repo skills (overrides if same skill name exists)

### Smart Deployment Behavior

When the container starts, the entrypoint script:

1. Checks if `/root/.openclaw/repo-skills/` exists (creates if not)
2. Iterates through all skills in the mounted repo-skills directory
3. For each skill:
   - **First deploy**: Copies skill to `/root/.openclaw/repo-skills/`
   - **Subsequent deploys**: **SKIPS** if skill already exists (preserves agent modifications)
   - **Force overwrite**: If skill name is in `FORCE_OVERWRITE_SKILLS` env var, overwrites it

### Force Overwriting Repo Skills

To reset a repo skill to its original version (discarding agent modifications):

**Via GitHub Actions:**
1. Go to Actions → deploy-to-home-server
2. Click "Run workflow"
3. Enter skill names in `force_overwrite_skills` field: `pdf-extractor,web-scraper`
4. Run workflow

**Via .env file:**
```bash
# In .env file:
FORCE_OVERWRITE_SKILLS=pdf-extractor,web-scraper

# Then restart:
docker-compose up -d
```

**Manually:**
```bash
# Delete specific skill
docker-compose exec openclaw rm -rf /root/.openclaw/repo-skills/pdf-extractor

# Or delete all repo skills to reset everything
docker-compose exec openclaw rm -rf /root/.openclaw/repo-skills/*

# Then restart to redeploy
docker-compose restart
```

### Skill Priority

When OpenClaw loads skills, it uses the configuration in `config/openclaw.json`:

```json5
skills: {
  load: {
    extraDirs: [
      "/root/.openclaw/repo-skills",  // Loaded first (base)
      "/root/.openclaw/skills",       // Loaded second (overrides)
    ]
  }
}
```

If both directories contain a skill with the same name, the **runtime skill wins** (loaded last).

## Current Implementation

The only skill currently implemented is **PDF Extractor** at `skills/pdf-extractor/`.

- **Purpose**: Extracts data from Brazilian credit card invoice PDFs
- **Input**: PDF file path or raw text (via stdin)
- **Output**: JSON with extracted expenses
- **Implementation**: Python script at `scripts/pdf_extractor.py`
- **Categories**: pdf, finance, extraction, brazilian

## Skill Structure

Each skill follows this directory structure:

```
skills/<skill-name>/
├── SKILL.md          # Documentation with frontmatter metadata
└── <skill-name>      # Executable script (any language)
```

### Frontmatter Format

The `SKILL.md` file must begin with YAML frontmatter:

```yaml
---
name: skill-name
description: Brief description of what the skill does
categories:
  - category1
  - category2
---
```

**Frontmatter Fields:**
- `name` (required): Unique skill identifier
- `description` (required): Human-readable description
- `categories` (optional): List of categories for organization

### Skill Interface

**Input:**
- Skills read input from `stdin`
- Input is typically JSON, but can be any format
- No command-line arguments (use stdin instead)

**Output:**
- Skills must output JSON to `stdout`
- JSON structure should indicate success/failure
- Include meaningful error messages for debugging

**Error Handling:**
- Return appropriate exit codes (0 for success, non-zero for error)
- Output error information in JSON format
- Include usage information when input is missing or invalid

## Development Commands

### Creating a New Skill

**1. Create skill directory:**
```bash
mkdir -p skills/my-skill
cd skills/my-skill
```

**2. Create SKILL.md:**
```markdown
---
name: my-skill
description: Description of my skill
categories:
  - category1
  - category2
---

# My Skill

Detailed documentation here...

## Usage

```bash
echo '{"key": "value"}' | my-skill
```

## Input

JSON object with the following fields:
- `field1`: Description

## Output

JSON object with the following fields:
- `result`: Description
```

**3. Create executable script:**

Bash example:
```bash
#!/bin/bash
# Read JSON input from stdin
input=$(cat)

# Parse input (using jq or manual parsing)
# ...

# Process input
# ...

# Output JSON result
echo '{"success": true, "result": "processed"}'
```

Python example:
```python
#!/usr/bin/env python3
import json
import sys

def main():
    # Read input from stdin
    input_data = sys.stdin.read()
    
    # Parse JSON
    try:
        data = json.loads(input_data) if input_data else {}
    except json.JSONDecodeError as e:
        error_result = {
            'success': False,
            'error': f'Invalid JSON input: {str(e)}'
        }
        print(json.dumps(error_result))
        sys.exit(1)
    
    # Process input
    # ...
    
    # Output result
    result = {
        'success': True,
        'data': data
    }
    print(json.dumps(result, ensure_ascii=False))

if __name__ == '__main__':
    main()
```

**4. Make executable:**
```bash
chmod +x skills/my-skill/my-skill
```

**5. Add to OpenClaw configuration:**

Edit `config/openclaw.json`:
```json5
skills: {
  entries: {
    "my-skill": {
      enabled: true,
    },
  },
}
```

**6. Rebuild and restart:**
```bash
docker-compose build
docker-compose up -d
```

### Testing Skills

**Local Testing:**
```bash
# Test with JSON input
echo '{"test": "data"}' | python3 my-script.py

# Test with file input
cat input.json | my-script.py

# Test error handling
echo "" | my-script.py
```

**Container Testing:**
```bash
# Copy skill to container
docker-compose cp skills/my-skill/ openclaw:/root/.openclaw/skills/my-skill/

# Test in container
echo '{"test": "data"}' | docker-compose exec -T openclaw /root/.openclaw/skills/my-skill/my-skill

# View skill output
docker-compose exec openclaw cat /root/.openclaw/skills/my-skill/my-skill
```

**Testing with OpenClaw:**
```bash
# Validate skill is loaded
docker-compose exec openclaw openclaw skills list

# Get skill info
docker-compose exec openclaw openclaw skills info my-skill

# Test skill execution
docker-compose exec openclaw openclaw skills run my-skill '{"test": "data"}'
```

## Skill Development Best Practices

### Input Handling

**1. Validate Input:**
```python
input_data = sys.stdin.read()
if not input_data:
    error_result = {
        'success': False,
        'error': 'No input provided',
        'usage': 'Provide JSON input via stdin'
    }
    print(json.dumps(error_result))
    sys.exit(1)
```

**2. Parse JSON Safely:**
```python
try:
    data = json.loads(input_data)
except json.JSONDecodeError as e:
    error_result = {
        'success': False,
        'error': f'Invalid JSON: {str(e)}'
    }
    print(json.dumps(error_result))
    sys.exit(1)
```

**3. Handle Missing Fields:**
```python
required_fields = ['field1', 'field2']
for field in required_fields:
    if field not in data:
        error_result = {
            'success': False,
            'error': f'Missing required field: {field}'
        }
        print(json.dumps(error_result))
        sys.exit(1)
```

### Output Formatting

**1. Always Return JSON:**
```python
result = {
    'success': True,
    'data': processed_data
}
print(json.dumps(result, ensure_ascii=False, indent=2))
```

**2. Include Metadata:**
```python
result = {
    'success': True,
    'data': data,
    'metadata': {
        'timestamp': datetime.now().isoformat(),
        'version': '1.0.0'
    }
}
```

**3. Error Responses:**
```python
error_result = {
    'success': False,
    'error': 'Descriptive error message',
    'details': 'Additional context'
}
print(json.dumps(error_result, ensure_ascii=False))
sys.exit(1)
```

### Error Handling

**1. Use Try-Except Blocks:**
```python
try:
    # Process data
    result = process_data(data)
except Exception as e:
    error_result = {
        'success': False,
        'error': str(e),
        'traceback': traceback.format_exc()
    }
    print(json.dumps(error_result, ensure_ascii=False))
    sys.exit(1)
```

**2. Log Errors (optional):**
```python
import logging
logging.basicConfig(level=logging.ERROR)
logging.error(f"Error processing data: {str(e)}")
```

**3. Graceful Degradation:**
```python
# Continue processing even if some data is invalid
valid_data = []
for item in data:
    try:
        processed = process_item(item)
        valid_data.append(processed)
    except Exception as e:
        # Log but continue
        print(f"Skipping invalid item: {str(e)}", file=sys.stderr)
```

### Performance

**1. Stream Large Inputs:**
```python
import json
import ijson

# Process large JSON files without loading entire file into memory
with open('large_file.json', 'r') as f:
    for item in ijson.items(f, 'item'):
        process_item(item)
```

**2. Cache Results:**
```python
import hashlib
import pickle

def get_cache_key(data):
    return hashlib.md5(json.dumps(data).encode()).hexdigest()

# Check cache
cache_key = get_cache_key(input_data)
if os.path.exists(f'/tmp/cache/{cache_key}'):
    result = pickle.load(open(f'/tmp/cache/{cache_key}', 'rb'))
else:
    # Process and cache
    result = process_data(input_data)
    pickle.dump(result, open(f'/tmp/cache/{cache_key}', 'wb'))
```

**3. Use Efficient Libraries:**
- `pymupdf` for PDF processing (faster than PyPDF2)
- `pandas` for data manipulation
- `numpy` for numerical operations

## Skill Testing

### Unit Testing

Create test files in `skills/<skill-name>/tests/`:

```python
# test_my_skill.py
import json
import subprocess
import pytest

def test_basic_functionality():
    input_data = {'test': 'data'}
    result = subprocess.run(
        ['my-skill'],
        input=json.dumps(input_data).encode(),
        capture_output=True
    )
    output = json.loads(result.stdout)
    assert output['success'] is True
    assert 'data' in output

def test_error_handling():
    result = subprocess.run(
        ['my-skill'],
        input=b'invalid json',
        capture_output=True
    )
    output = json.loads(result.stdout)
    assert output['success'] is False
    assert 'error' in output
```

Run tests:
```bash
pytest skills/my-skill/tests/
```

### Integration Testing

Test skill with OpenClaw:

```bash
# Add test prompt to configuration
echo '{"role": "user", "content": "Use my-skill to process this data: {"test": "value"}}' | \
  docker-compose exec -T openclaw openclaw chat josemar
```

### Performance Testing

```bash
# Test with large input
dd if=/dev/urandom bs=1M count=10 | \
  docker-compose exec -T openclaw /root/.openclaw/skills/my-skill/my-skill

# Measure execution time
time echo '{"test": "data"}' | my-skill
```

## Skill Packaging

### Using Python Dependencies

If your skill requires Python dependencies:

1. **Add to Dockerfile**:
```dockerfile
# In Dockerfile
RUN pip3 install --no-cache-dir your-dependency
```

2. **Rebuild image**:
```bash
docker-compose build
```

3. **Test dependency**:
```bash
docker-compose exec openclaw python3 -c "import your_dependency; print(your_dependency.__version__)"
```

### Using System Dependencies

If your skill requires system libraries:

1. **Add to Dockerfile**:
```dockerfile
# In Dockerfile
RUN apk add --no-cache your-library
```

2. **Rebuild image**:
```bash
docker-compose build
```

3. **Test dependency**:
```bash
docker-compose exec openclaw your-command --version
```

### Bundling Dependencies

For skills with many dependencies, consider:
- Creating a separate Docker image
- Using a virtual environment
- Including requirements.txt in skill directory

## Skill Configuration

### Configuration Files

Skills can have configuration files in their directory:

```
skills/my-skill/
├── SKILL.md
├── my-skill
└── config.json5  # Skill configuration
```

Read configuration in skill:
```python
import json5

config_path = '/root/.openclaw/skills/my-skill/config.json5'
with open(config_path, 'r') as f:
    config = json5.load(f)
```

### Environment Variables

Skills can access environment variables:

```python
import os
api_key = os.environ.get('MY_SKILL_API_KEY')
```

Environment variables can be set in `docker-compose.yml`:
```yaml
environment:
  - MY_SKILL_API_KEY=${MY_SKILL_API_KEY}
```

## Skill Debugging

### Enable Debug Output

Add debug flag to skill:
```python
import sys

if '--debug' in sys.argv or 'DEBUG' in os.environ:
    debug = True
else:
    debug = False

if debug:
    print(f"Debug: Input data: {data}", file=sys.stderr)
```

### View Skill Logs

```bash
# Follow logs
docker-compose logs -f openclaw

# Search for skill errors
docker-compose logs openclaw | grep "my-skill"

# View recent errors
docker-compose logs --tail=100 openclaw | grep -i error
```

### Test Skill Manually

```bash
# Enter container shell
docker-compose exec openclaw sh

# Test skill directly
cd /root/.openclaw/skills/my-skill
echo '{"test": "data"}' | ./my-skill

# Check permissions
ls -la /root/.openclaw/skills/my-skill/

# Check dependencies
python3 -c "import required_module; print(required_module.__version__)"
```

## Skill Security

### Input Sanitization

Always sanitize input to prevent injection attacks:

```python
import re

def sanitize_filename(filename):
    # Remove dangerous characters
    safe_filename = re.sub(r'[^\w\-_\. ]', '', filename)
    return safe_filename
```

### Output Validation

Validate output before returning:

```python
def validate_output(data):
    # Ensure required fields are present
    if not isinstance(data, dict):
        raise ValueError("Output must be a dictionary")
    
    # Sanitize sensitive data
    if 'password' in data:
        data['password'] = '***REDACTED***'
    
    return data
```

### Resource Limits

Prevent resource exhaustion:

```python
import resource

# Limit memory usage
def set_memory_limit(max_memory_mb):
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (max_memory_mb * 1024 * 1024, hard))

# Limit execution time
import signal

def timeout_handler(signum, frame):
    raise TimeoutError("Skill execution timed out")

signal.signal(signal.SIGALRM, timeout_handler)
signal.alarm(30)  # 30 second timeout
```

## Skill Examples

### Example: File Processor

```python
#!/usr/bin/env python3
import json
import sys
import os
import hashlib

def process_file(file_path):
    if not os.path.exists(file_path):
        return {'error': 'File not found'}
    
    # Calculate file hash
    with open(file_path, 'rb') as f:
        file_hash = hashlib.sha256(f.read()).hexdigest()
    
    # Get file stats
    file_stats = os.stat(file_path)
    
    return {
        'hash': file_hash,
        'size': file_stats.st_size,
        'modified': file_stats.st_mtime,
        'path': file_path
    }

def main():
    input_data = json.loads(sys.stdin.read())
    file_path = input_data.get('path')
    
    if not file_path:
        print(json.dumps({'success': False, 'error': 'File path is required'}))
        sys.exit(1)
    
    try:
        result = process_file(file_path)
        print(json.dumps({'success': True, 'data': result}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({'success': False, 'error': str(e)}), ensure_ascii=False)
        sys.exit(1)

if __name__ == '__main__':
    main()
```

## Testing Skills

### Local Testing

Test skills locally before deploying to the container:

**Test with Python script directly:**
```bash
# For PDF extractor
echo "/path/to/test.pdf" | python3 scripts/pdf_extractor.py

# For any skill
echo '{"input": "test data"}' | python3 scripts/your_skill.py
```

**Test with raw text input:**
```bash
# PDF extractor with text
echo "10/12 UBER TRIP 32,75" | python3 scripts/pdf_extractor.py

# JSON input
echo '{"file": "/tmp/test.txt"}' | python3 scripts/your_skill.py
```

**Check Python dependencies:**
```bash
# Verify pymupdf is installed
python3 -c "import pymupdf; print(pymupdf.__version__)"

# Check all required modules
python3 -c "import sys; print(sys.path)"
```

### Container Testing

Test skills in the Docker environment:

**Test repo skill:**
```bash
# Test deployed repo skill
echo "/workspace/test.pdf" | docker-compose run --rm -T openclaw /root/.openclaw/repo-skills/pdf-extractor/pdf-extractor
```

**Test runtime skill:**
```bash
# Test assistant-created skill
echo '{"input": "test"}' | docker-compose run --rm -T openclaw /root/.openclaw/skills/<skill-name>/<skill-name>
```

**Debug skill execution:**
```bash
# View skill script
docker-compose exec openclaw cat /root/.openclaw/repo-skills/pdf-extractor/pdf-extractor

# Check skill permissions
docker-compose exec openclaw ls -la /root/.openclaw/repo-skills/pdf-extractor/

# Test with verbose output
echo "test" | docker-compose exec -T openclaw sh -x /root/.openclaw/repo-skills/pdf-extractor/pdf-extractor
```

**Check Python in container:**
```bash
# Verify Python and modules
docker-compose exec openclaw python3 --version
docker-compose exec openclaw python3 -c "import pymupdf; print('pymupdf OK')"

# Check skill script syntax
docker-compose exec openclaw python3 -m py_compile /root/.openclaw/repo-skills/pdf-extractor/pdf-extractor
```

### Debugging Skills

**Enable debug logging in OpenClaw:**
```bash
# Edit .env
OPENCLAW_LOG_LEVEL=debug

# Restart and watch logs
docker-compose restart openclaw
docker-compose logs -f openclaw | grep -i skill
```

**Check skill loading:**
```bash
# List all available skills
docker-compose exec openclaw openclaw skills list

# Get skill info
docker-compose exec openclaw openclaw skills info pdf-extractor

# Check which directory skill loads from
docker-compose exec openclaw ls -la /root/.openclaw/repo-skills/
docker-compose exec openclaw ls -la /root/.openclaw/skills/
```

**Common skill issues:**
- **Permission denied**: Check executable bit (`chmod +x`)
- **Module not found**: Verify Python dependencies in Dockerfile
- **Invalid JSON output**: Check stdout is clean JSON only
- **Path not found**: Ensure paths are accessible within container

## Additional Resources

- **OpenClaw Skills Documentation**: https://docs.openclaw.dev/skills
- **OpenClaw Skill Examples**: https://github.com/openclaw/skills
- **JSON5 Format**: https://json5.org
- **Frontmatter Specification**: https://jekyllrb.com/docs/front-matter/

## Support

For skill development issues:
1. Check skill logs: `docker-compose logs -f openclaw`
2. Test skill manually: `echo '{"test": "data"}' | my-skill`
3. Review this documentation and skill examples
4. Check OpenClaw documentation at https://docs.openclaw.dev
