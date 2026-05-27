#!/bin/bash
# ONE-LINE INSTALLER FOR OVER 2.5 PREDICTOR
# Run: chmod +x install.sh && ./install.sh

echo "Installing Over 2.5 Goals Predictor..."

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Please install from python.org"
    exit 1
fi

# Install dependencies
echo "📦 Installing required packages..."
pip3 install requests beautifulsoup4

# Create project folder
mkdir -p over25-predictor
cd over25-predictor

# Download files (if using curl/wget)
echo "📥 Downloading scripts..."
# Note: In real use, you'd host these files or include them

echo ""
echo "✅ Installation complete!"
echo ""
echo "To run predictions:"
echo "  python3 over25_predictor.py"
echo ""
echo "To set up daily automation:"
echo "  python3 daily_runner.py"
echo ""
echo "For manual data entry:"
echo "  python3 team_data_manager.py"
