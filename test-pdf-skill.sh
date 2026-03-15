#!/bin/bash
# Test the PDF extraction skill

echo "Testing PDF extraction skill..."

# Create a test input (simulated PDF text)
TEST_INPUT="PEDRO HENRIQUE GOMES VENTUROTT 1.234,56
Valor total da Fatura: R$ 1.234,56

10/12 UBER   TRIP   32,75
11/12 IFFOOD ORDER  85,50
12/12 PAGAMENTO DB DIRETO CONTA 1.234,56"

echo "Test input:"
echo "---"
echo "$TEST_INPUT"
echo "---"

# Test the Python script directly
echo ""
echo "Running PDF extractor script..."
echo "$TEST_INPUT" | python3 scripts/pdf_extractor.py
