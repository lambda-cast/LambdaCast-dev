#!/bin/bash
# Quick diagnostic script to check ML models availability

echo "═══════════════════════════════════════════════"
echo "  LambdaCast Models Diagnostic"
echo "═══════════════════════════════════════════════"
echo ""

# Check if models directory exists
if [ ! -d "models" ]; then
    echo "❌ ERROR: models/ directory not found!"
    exit 1
fi

echo "✅ models/ directory exists"
echo ""

# List models
echo "📦 Available models:"
echo "---"
model_count=0
for ext in joblib pkl; do
    while IFS= read -r file; do
        if [ -f "$file" ]; then
            size=$(du -h "$file" | cut -f1)
            model_count=$((model_count + 1))
            echo "  $model_count. $(basename "$file") ($size)"
        fi
    done < <(find models -maxdepth 1 -name "*.$ext" 2>/dev/null)
done

if [ $model_count -eq 0 ]; then
    echo "  ⚠️  No models found!"
    echo ""
    echo "Expected model files:"
    echo "  - models/xgb_PV1_Power_W_1.joblib"
    echo "  - models/arx_model.pkl"
    echo ""
    echo "Please add trained models to the models/ directory."
    exit 1
fi

echo ""
echo "✅ Found $model_count model(s)"
echo ""

# Check if Docker container can access models
echo "🐳 Checking Docker container access..."
if [ "$(docker ps -q -f name=lambdacastt)" ]; then
    echo "  Container is running"
    echo ""
    echo "  Models visible inside container:"
    docker exec lambdacastt ls -lh /models 2>/dev/null | grep -E '\.(joblib|pkl)$' || echo "  ⚠️  No models visible in container!"
else
    echo "  ⚠️  Container not running (run 'docker-compose up -d' first)"
fi

echo ""
echo "═══════════════════════════════════════════════"
echo "  Status: OK ($model_count models ready)"
echo "═══════════════════════════════════════════════"
