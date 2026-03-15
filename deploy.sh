#!/bin/bash
# Deployment script for Josemar Assistente

set -e

echo "🚀 Deploying Josemar Assistente..."

# Check if .env file exists
if [ ! -f .env ]; then
    echo "⚠️  .env file not found. Creating from example..."
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "📋 Created .env file from example."
        echo "   Please edit .env with your API keys before continuing."
        exit 1
    else
        echo "❌ .env.example not found. Please create a .env file manually."
        exit 1
    fi
fi

# Check if Docker Compose is available
if command -v docker compose &> /dev/null; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
else
    echo "❌ Docker Compose not found. Please install Docker and Docker Compose."
    echo "   Installation: https://docs.docker.com/compose/install/"
    exit 1
fi

echo "🐳 Using Docker Compose command: ${COMPOSE_CMD}"

# Build and start services
echo "🔨 Building Docker image..."
${COMPOSE_CMD} build

echo "🚀 Starting services..."
${COMPOSE_CMD} up -d

echo "⏳ Waiting for services to start..."
sleep 10

# Check if service is running
if ${COMPOSE_CMD} ps | grep -q "Up"; then
    echo "✅ Josemar Assistente is running!"
    echo ""
    echo "📋 Next steps:"
    echo "1. Check logs: ${COMPOSE_CMD} logs -f"
    echo "2. Stop service: ${COMPOSE_CMD} down"
    echo "3. Update configuration: edit config/openclaw.json5 then ${COMPOSE_CMD} restart"
    echo ""
    echo "💬 Start chatting with your Telegram bot!"
else
    echo "❌ Service failed to start. Check logs:"
    ${COMPOSE_CMD} logs
    exit 1
fi
