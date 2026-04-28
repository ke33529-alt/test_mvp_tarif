import os
import re
from typing import List, Dict

class LegalDocumentChunker:
    """Умный чанкер для юридических документов. Делит по предложениям, сохраняет chunk_index для поиска соседей."""
    
    def __init__(self, max_chunk_chars: int = 500, neighbor_radius: int = 4):
        self.max_chunk_chars = max_chunk_chars
        self.neighbor_radius = neighbor_radius

    def chunk_text(self, text: str, doc_id: str, metadata: Dict = None) -> List[Dict]:
        if metadata is None:
            metadata = {}
            
        # Защита распространённых сокращений от ложного разреза
        protected = text
        abbr_map = {
            'т.д.': '__ABBR_TD__', 'т.п.': '__ABBR_TP__', 'РФ': '__ABBR_RF__',
            'и т.д.': '__ABBR_ITD__', 'и т.п.': '__ABBR_ITP__', '№': '__ABBR_N__'
        }
        for orig, ph in abbr_map.items():
            protected = protected.replace(orig, ph)
            
        sentences = re.split(r'(?<=[.!?])\s+', protected)
        for k, v in abbr_map.items():
            sentences = [s.replace(v, k) for s in sentences]
            
        chunks = []
        current_sentences = []
        current_len = 0
        chunk_idx = 0

        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
                
            sent_len = len(sent) + 1
            
            # Если предложение длиннее лимита → режем по пробелам
            if sent_len > self.max_chunk_chars:
                parts = []
                temp = sent
                while len(temp) > self.max_chunk_chars:
                    split_at = temp.rfind(' ', 0, self.max_chunk_chars)
                    if split_at == -1:
                        split_at = self.max_chunk_chars
                    parts.append(temp[:split_at].strip())
                    temp = temp[split_at:].strip()
                if temp:
                    parts.append(temp)
                for part in parts:
                    chunks.append({
                        "text": part,
                        "metadata": {**metadata, "doc_id": doc_id, "chunk_index": chunk_idx}
                    })
                    chunk_idx += 1
                continue

            # Если добавление превысит лимит → сохраняем текущий чанк
            if current_len + sent_len > self.max_chunk_chars and current_sentences:
                chunks.append({
                    "text": " ".join(current_sentences),
                    "metadata": {**metadata, "doc_id": doc_id, "chunk_index": chunk_idx}
                })
                chunk_idx += 1
                current_sentences = [sent]
                current_len = sent_len
            else:
                current_sentences.append(sent)
                current_len += sent_len

        if current_sentences:
            chunks.append({
                "text": " ".join(current_sentences),
                "metadata": {**metadata, "doc_id": doc_id, "chunk_index": chunk_idx}
            })
        return chunks


def retrieve_with_neighbors(query: str, collection, top_k: int = 3, neighbor_radius: int = 4) -> List[Dict]:
    """Поиск в ChromaDB + добавление соседей (до и после)"""
    results = collection.query(
        query_texts=[query],
        n_results=top_k,
        include=["documents", "metadatas", "distances"]
    )
    
    seen = set()
    expanded = []
    
    for doc, meta, dist in zip(results['documents'][0], results['metadatas'][0], results['distances'][0]):
        doc_id = meta.get('doc_id')
        chunk_idx = meta.get('chunk_index', 0)
        
        for offset in range(-neighbor_radius, neighbor_radius + 1):
            key = (doc_id, chunk_idx + offset)
            if key in seen:
                continue
            seen.add(key)
            
            neighbor_res = collection.get(
                where={"doc_id": doc_id, "chunk_index": chunk_idx + offset},
                include=["documents", "metadatas"]
            )
            if neighbor_res['documents']:
                expanded.append({
                    "text": neighbor_res['documents'][0],
                    "metadata": neighbor_res['metadatas'][0],
                    "is_target": offset == 0
                })
    return expanded


def build_context_with_neighbors(query: str, collection, top_k: int = 3, neighbor_radius: int = 4) -> str:
    """Собирает строку контекста с пометками [🎯 ЦЕЛЕВОЙ] / [СОСЕД]"""
    chunks = retrieve_with_neighbors(query, collection, top_k, neighbor_radius)
    if not chunks:
        return ""
        
    parts = []
    for c in chunks:
        meta = c['metadata']
        file_info = meta.get('filename', 'Неизвестно')
        if meta.get('page'):
            file_info += f" (стр. {meta['page']})"
        label = "[🎯 ЦЕЛЕВОЙ]" if c['is_target'] else "[СОСЕД]"
        parts.append(f"{label} {file_info}:\n{c['text']}")
        
    return "\n\n---\n\n".join(parts)


if __name__ == "__main__":
    print("🧪 Тест чанкера...")
    chunker = LegalDocumentChunker()
    test_text = "П. 12. Тариф на тепловую энергию устанавливается на основе экономически обоснованных расходов. П. 13. В состав расходов включаются: топливо, заработная плата, амортизация, ремонт. П. 14. Расходы на представительские мероприятия не включаются в тариф."
    chunks = chunker.chunk_text(test_text, doc_id="test_doc", metadata={"filename": "test.txt"})
    print(f"✅ Создано чанков: {len(chunks)}")
    for c in chunks:
        print(f"[{c['metadata']['chunk_index']}] {c['text']}")