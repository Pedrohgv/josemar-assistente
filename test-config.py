#!/usr/bin/env python3
"""
Configuration validation for Josemar Assistente OpenClaw bot.
Validates openclaw.json5 structure and content.
"""

import json
import os
import sys


def test_config():
    """Validate OpenClaw configuration structure."""
    config_path = "config/openclaw.json5"

    if not os.path.exists(config_path):
        print(f"❌ Configuration file not found: {config_path}")
        return False

    try:
        # Read file
        with open(config_path, 'r') as f:
            content = f.read()

        # Remove JSON5 comments for basic validation
        lines = content.split('\n')
        cleaned_lines = []
        for line in lines:
            line = line.strip()
            if line.startswith('//'):
                continue
            if '//' in line:
                line = line.split('//')[0]
            cleaned_lines.append(line)

        cleaned_content = '\n'.join(cleaned_lines)

        # Try to parse as JSON (JSON5 is a superset)
        try:
            import json5
            config = json5.loads(content)
            print("✅ Configuration parsed successfully with json5")
        except ImportError:
            print("⚠️  json5 not installed, trying basic JSON parse...")
            try:
                config = json.loads(cleaned_content)
                print("✅ Configuration parsed successfully with JSON")
            except json.JSONDecodeError as e:
                print(f"❌ JSON parsing error: {e}")
                return False

        # Check required sections for OpenClaw configuration
        required_sections = ['env', 'agents', 'channels']
        for section in required_sections:
            if section not in config:
                print(f"❌ Missing required section: {section}")
                return False

        print("✅ All required sections present")

        # Validate env section
        if 'env' in config:
            env_vars = ['ZAI_API_KEY', 'TELEGRAM_BOT_TOKEN']
            for var in env_vars:
                if var not in config['env']:
                    print(f"⚠️  Environment variable not defined: {var}")

        # Validate agents section
        if 'agents' in config:
            if 'list' not in config['agents']:
                print("❌ agents.list not defined")
                return False

            # Check for josemar agent
            josemar_found = False
            for agent in config['agents']['list']:
                if agent.get('id') == 'josemar':
                    josemar_found = True
                    print("✅ Josemar agent found")
                    # Check for required fields
                    required_agent_fields = ['id', 'name', 'workspace', 'model']
                    for field in required_agent_fields:
                        if field not in agent:
                            print(f"⚠️  Agent missing field: {field}")
                    break

            if not josemar_found:
                print("❌ Josemar agent not found in agents.list")
                return False

        # Validate channels section
        if 'channels' in config:
            if 'telegram' not in config['channels']:
                print("⚠️  Telegram channel not configured")
            else:
                telegram_config = config['channels']['telegram']
                if telegram_config.get('enabled'):
                    print("✅ Telegram channel enabled")
                    if telegram_config.get('language') == 'pt-BR':
                        print("✅ Brazilian Portuguese configured")
                else:
                    print("⚠️  Telegram channel disabled")

        # Validate models section (for custom providers)
        if 'models' in config:
            if 'providers' in config['models']:
                providers = config['models']['providers']
                if 'deepseek' in providers:
                    print("✅ DeepSeek provider configured")
                else:
                    print("⚠️  DeepSeek provider not configured (optional)")
            else:
                print("⚠️  models.providers not defined (DeepSeek not available)")

        # Validate skills section
        if 'skills' in config:
            if 'entries' in config['skills']:
                skills = config['skills']['entries']
                if 'pdf-extractor' in skills:
                    print("✅ PDF extractor skill configured")
                else:
                    print("⚠️  PDF extractor skill not in skills.entries")
            else:
                print("⚠️  skills.entries not defined")

        # Validate prompts section (Portuguese)
        if 'prompts' in config:
            if 'josemar' in config['prompts']:
                print("✅ Josemar system prompt configured")
                # Check for Portuguese keywords
                prompt = config['prompts']['josemar']
                portuguese_keywords = ['português', 'brasileiro', 'Português Brasileiro']
                has_portuguese = any(keyword.lower() in prompt.lower() for keyword in portuguese_keywords)
                if has_portuguese:
                    print("✅ Portuguese language configured in system prompt")
                else:
                    print("⚠️  Portuguese language not clearly specified in system prompt")

        print("\n✅ Configuration structure looks good!")
        return True

    except Exception as e:
        print(f"❌ Error testing configuration: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Main entry point."""
    print("🔍 Validating OpenClaw configuration...")
    print("=" * 50)
    success = test_config()
    print("=" * 50)

    if success:
        print("\n✅ All checks passed!")
        print("\nNext steps:")
        print("1. Ensure .env file exists with API keys")
        print("2. Build Docker image: docker-compose build")
        print("3. Start services: docker-compose up -d")
        print("4. Check logs: docker-compose logs -f")
    else:
        print("\n❌ Configuration validation failed!")
        print("\nPlease fix the errors above before deploying.")

    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
