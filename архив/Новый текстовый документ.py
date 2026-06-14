# =============================================================================
# 🦙 Тест подключения к Ollama
# =============================================================================

# 1. Переход в директорию проекта
$projectPath = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectPath
Write-Host "📁 Проект: $projectPath" -ForegroundColor Cyan

# 2. Активация виртуального окружения
$venvActivate = Join-Path $projectPath "venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    Write-Host "📦 Активация venv..." -ForegroundColor Cyan
    & $venvActivate
    Write-Host "✅ venv активирован" -ForegroundColor Green
} else {
    Write-Host "❌ venv не найден: $venvActivate" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  🦙 Тест подключения к Ollama" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 3. Тест через Python (с отключением предупреждений)
python -W ignore -c "
import requests
import json
import os
import warnings

# Отключение предупреждений
os.environ['ANONYMIZED_TELEMETRY'] = 'false'
warnings.filterwarnings('ignore')

print('🔍 Проверка подключения к Ollama...')
print('')

try:
    # Тест 1: Проверка сервера
    print('1️⃣ Проверка сервера (GET /api/tags)...')
    response = requests.get('http://localhost:11434/api/tags', timeout=10)
    
    if response.status_code == 200:
        data = response.json()
        models = data.get('models', [])
        print('   ✅ Сервер отвечает')
        print(f'   📦 Найдено моделей: {len(models)}')
        for m in models:
            print(f'      • {m[\"name\"]} ({m.get(\"size\", 0) / 1e9:.1f} GB)')
    else:
        print(f'   ❌ Статус: {response.status_code}')
        exit(1)
    
    print('')
    
    # Тест 2: Генерация ответа
    print('2️⃣ Тест генерации (POST /api/generate)...')
    test_prompt = 'Назови 3 документа для тарифной заявки в РФ'
    
    response = requests.post(
        'http://localhost:11434/api/generate',
        json={
            'model': 'llama3',
            'prompt': test_prompt,
            'stream': False
        },
        timeout=120
    )
    
    if response.status_code == 200:
        result = response.json()
        answer = result.get('response', '')
        print('   ✅ Ответ получен')
        print(f'   📝 Текст: {answer[:150]}...' if len(answer) > 150 else f'   📝 Текст: {answer}')
    else:
        print(f'   ❌ Статус: {response.status_code}')
        print(f'   💬 Ответ: {response.text[:200]}')
    
    print('')
    
    # Тест 3: Советчик (интеграционный)
    print('3️⃣ Интеграционный тест (core.advisor)...')
    from core.advisor import ask_question
    
    result = ask_question('Какие документы нужны для тарифной заявки?')
    
    print(f'   ✅ Ответ: {result[\"answer\"][:100]}...' if len(result['answer']) > 100 else f'   ✅ Ответ: {result[\"answer\"]}')
    print(f'   📚 Источники: {len(result[\"sources\"])}')
    print(f'   🔄 Редирект: {result.get(\"redirect\", \"Нет\")}')
    
    print('')
    print('========================================')
    print('  🎉 Все тесты пройдены!')
    print('========================================')
    
except requests.exceptions.ConnectionError:
    print('   ❌ Ошибка: Не удалось подключиться к localhost:11434')
    print('   💡 Решение: Запусти \"ollama serve\" в отдельном окне')
    exit(1)
    
except requests.exceptions.ReadTimeout:
    print('   ⏱️  Ошибка: Таймаут ответа от Ollama')
    print('   💡 Решение: Ollama работает, но медленно — увеличь timeout')
    exit(1)
    
except Exception as e:
    print(f'   ❌ Ошибка: {type(e).__name__}: {e}')
    exit(1)
"