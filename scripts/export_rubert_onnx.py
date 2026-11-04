"""Готовит модель для семантического поиска: cointegrated/rubert-tiny2 → ONNX.

    python scripts/export_rubert_onnx.py <папка> [--revision <коммит на Hugging Face>]

На Hugging Face у модели есть только веса PyTorch, поэтому один раз переводим её в ONNX. Для этого
нужны torch и transformers (requirements-ml-build.txt) — но только здесь, при сборке. Сервису потом
хватает onnxruntime и tokenizers (requirements-ml.txt): образ легче примерно на гигабайт.

В папке появятся model.onnx (выход — last_hidden_state), tokenizer.json, 1_Pooling/config.json
(у rubert-tiny2 вектор фразы — токен CLS) и MODEL_INFO.txt с источником и коммитом.
В конце скрипт сверяет ONNX с исходной моделью на нескольких фразах и падает, если векторы разошлись.
В Dockerfile это делает стадия model при WITH_EMBEDDINGS=1.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

MODEL = "cointegrated/rubert-tiny2"
# Коммит модели закреплён: сборка воспроизводима, а пороги косинуса подобраны под конкретные веса
REVISION = "e8ed3b0c8bbf4fb6984c3de043bf7d2f4e5969ae"

CHECK = [
    "Во сколько забирать с продлёнки?",
    "До какого часа работает ГПД?",
    "Какая форма на физкультуру?",
    "Сколько сдавать на экскурсию в музей?",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--revision", default=REVISION)
    args = ap.parse_args()

    import numpy as np
    import onnxruntime as ort
    import torch
    from huggingface_hub import snapshot_download
    from transformers import AutoModel, AutoTokenizer

    src = Path(snapshot_download(args.model, revision=args.revision, allow_patterns=[
        "config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
        "vocab.txt", "modules.json", "1_Pooling/config.json"]))
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    tok = AutoTokenizer.from_pretrained(src)
    model = AutoModel.from_pretrained(src).eval()

    class Hidden(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, input_ids, attention_mask, token_type_ids):
            return self.m(input_ids=input_ids, attention_mask=attention_mask,
                          token_type_ids=token_type_ids).last_hidden_state

    sample = tok(CHECK[:2], padding=True, return_tensors="pt")
    names = ["input_ids", "attention_mask", "token_type_ids"]
    axes = {n: {0: "batch", 1: "seq"} for n in names} | {"last_hidden_state": {0: "batch", 1: "seq"}}
    with torch.no_grad():
        torch.onnx.export(Hidden(model), tuple(sample[n] for n in names), str(out / "model.onnx"),
                          input_names=names, output_names=["last_hidden_state"], dynamic_axes=axes,
                          opset_version=17, do_constant_folding=True)

    shutil.copy(src / "tokenizer.json", out / "tokenizer.json")
    (out / "1_Pooling").mkdir(exist_ok=True)
    shutil.copy(src / "1_Pooling" / "config.json", out / "1_Pooling" / "config.json")
    shutil.copy(src / "config.json", out / "config.json")
    (out / "MODEL_INFO.txt").write_text(f"{args.model}@{args.revision}\nONNX opset 17, выход last_hidden_state, "
                                        "свёртка CLS, векторы нормируются\n", encoding="utf-8")

    # сверка: исходная модель (torch) против ONNX — тем же кодом, что у сервиса
    with torch.no_grad():
        enc = tok(CHECK, padding=True, truncation=True, max_length=128, return_tensors="pt")
        ref = model(**enc).last_hidden_state[:, 0, :]
        ref = torch.nn.functional.normalize(ref).numpy()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from app.services.semantic import OnnxEmbedder

        got = np.array(OnnxEmbedder(out).embed(CHECK))
    except ImportError:  # без кода сервиса рядом — проверяем голым onnxruntime
        sess = ort.InferenceSession(str(out / "model.onnx"), providers=["CPUExecutionProvider"])
        h = sess.run(None, {n: enc[n].numpy() for n in names})[0][:, 0, :]
        got = h / np.linalg.norm(h, axis=1, keepdims=True)
    diff = float(np.abs(ref - got).max())
    print(f"ONNX сохранён в {out}; расхождение с исходной моделью: {diff:.2e}")
    print(f"косинус «{CHECK[0]}» ↔ «{CHECK[1]}»: {float(got[0] @ got[1]):.3f}")
    print(f"косинус «{CHECK[0]}» ↔ «{CHECK[2]}»: {float(got[0] @ got[2]):.3f}")
    if diff > 1e-3:
        print("Векторы ONNX и исходной модели расходятся — экспорт неверный", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
