import chromadb
import os
from datetime import datetime
from core.chunker import LegalDocumentChunker

DB_PATH = "data/vector_db"
RAW_PATH = "data/raw"

def index_documents():
    print("🔄 Инициализация ChromaDB...")
    client = chromadb.PersistentClient(path=DB_PATH)
    try:
        collection = client.get_collection(name="tariff_docs")
        print("📁 Коллекция найдена")
    except Exception:
        collection = client.create_collection(name="tariff_docs")
        print("✅ Коллекция создана")

    chunker = LegalDocumentChunker()
    old_count = collection.count()
    print(f"📊 В базе до индексации: {old_count} чанков")
    added_chunks = 0

    if not os.path.exists(RAW_PATH):
        print(f"❌ Папка {RAW_PATH} не найдена!")
        return

    for category in os.listdir(RAW_PATH):
        category_path = os.path.join(RAW_PATH, category)
        if not os.path.isdir(category_path):
            continue

        print(f"\n📂 Категория: {category}")
        for filename in os.listdir(category_path):
            file_path = os.path.join(category_path, filename)

            # Пропускаем папки, служебные и уже проиндексированные файлы
            if not os.path.isfile(file_path) or filename.endswith(".indexed") or filename.startswith("."):
                continue

            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()

                if len(content.strip()) < 50:
                    continue

                doc_id = f"{category}_{os.path.splitext(filename)[0]}"
                metadata = {
                    "filename": filename,
                    "category": category,
                    "indexed_at": datetime.now().isoformat()
                }

                # Чанкинг новым умным чанкером
                chunks = chunker.chunk_text(text=content, doc_id=doc_id, metadata=metadata)

                # Удаляем старые данные файла (чистая перезапись)
                try:
                    collection.delete(where={"filename": filename})
                except Exception:
                    pass

                if chunks:
                    collection.add(
                        documents=[c['text'] for c in chunks],
                        metadatas=[c['metadata'] for c in chunks],
                        ids=[f"{doc_id}_c{i}" for i in range(len(chunks))]
                    )
                    added_chunks += len(chunks)
                    print(f"  ✅ {filename} ({len(chunks)} умных чанков)")

                # Помечаем как обработанный
                with open(file_path + ".indexed", 'w', encoding='utf-8') as f:
                    f.write(f"Indexed at {datetime.now().isoformat()} | Chunks: {len(chunks)}")

            except Exception as e:
                print(f"  ❌ {filename}: {e}")

    new_count = collection.count()
    print(f"\n{'='*50}")
    print(f"✅ Готово!")
    print(f"📊 Всего чанков: {new_count}")
    print(f"📈 Добавлено за сессию: {added_chunks}")
    print(f"{'='*50}")

    if new_count > 0:
        print("\n🔍 Тестовый поиск 'тариф':")
        try:
            res = collection.query(query_texts=["тариф"], n_results=3)
            for d, m in zip(res["documents"][0], res["metadatas"][0]):
                print(f"  📄 {m.get('filename', 'N/A')} (ч.{m.get('chunk_index', '?')}): {d[:80]}...")
        except Exception as e:
            print(f"  ❌ Ошибка поиска: {e}")

if __name__ == "__main__":
    index_documents()