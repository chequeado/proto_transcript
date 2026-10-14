"""Uso: python -m proto.compare data/sessions/<id> [--ref referencia.txt]

Deja texto-<motor>.txt y comparacion.md en la sesión. Con --ref (texto corregido por una persona) mide WER
(sustituciones / borrados / inserciones) y números acertados. Normaliza: minúsculas, sin tildes ni puntuación.
"""

import argparse
import itertools
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from .costs import cost


def load_texts(session_dir: Path) -> dict[str, str]:
    finals: dict[str, dict[str, str]] = {}
    for line in (session_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        finals.setdefault(d["engine"], {})[d["key"]] = d["text"]
    return {engine: " ".join(t.strip() for t in turns.values() if t.strip()) for engine, turns in finals.items()}


def normalize(text: str) -> list[str]:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"(?<=\d)[.,](?=\d)", "", text)  # 1.300 / 1,5 -> un solo número
    return re.sub(r"[^\w\s]", " ", text).split()


def edit_ops(ref: list[str], hyp: list[str]) -> Counter:
    """Distancia de edición por palabras, con conteo de sustituciones (S), borrados (D) e inserciones (I)."""
    prev = [(j, Counter(I=j)) for j in range(len(hyp) + 1)]
    for i in range(1, len(ref) + 1):
        cur = [(i, Counter(D=i))]
        for j in range(1, len(hyp) + 1):
            if ref[i - 1] == hyp[j - 1]:
                cur.append(prev[j - 1])
                continue
            options = [(prev[j - 1], "S"), (prev[j], "D"), (cur[j - 1], "I")]
            (dist, ops), op = min(options, key=lambda o: o[0][0])
            new = ops.copy()
            new[op] += 1
            cur.append((dist + 1, new))
        prev = cur
    return prev[-1][1]


def wer(ref: list[str], hyp: list[str]) -> tuple[float, Counter]:
    ops = edit_ops(ref, hyp)
    return (sum(ops.values()) / max(len(ref), 1)), ops


def numbers(words: list[str]) -> Counter:
    return Counter(w for w in words if any(c.isdigit() for c in w))


def engine_costs(session_dir: Path) -> dict[str, float]:
    out = {}
    for line in (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e["kind"] == "session_end":
            for name, x in (e["stats"].get("engines") or {}).items():
                usd = cost(name, x.get("model"), x.get("billed_s") or 0, speakers=x.get("speakers", False))
                if usd is not None:
                    out[name] = usd
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--ref", type=Path, help="texto de referencia corregido por una persona")
    args = ap.parse_args()

    texts = {e: t for e, t in load_texts(args.session_dir).items() if e != "null"}
    words = {e: normalize(t) for e, t in texts.items()}
    for engine, text in texts.items():
        (args.session_dir / f"texto-{engine}.txt").write_text(text + "\n", encoding="utf-8")
    costs = engine_costs(args.session_dir)

    lines = [f"# Comparación · {args.session_dir.name}", ""]
    lines += ["| Motor | Palabras | Costo de la sesión |", "|---|---|---|"]
    for e in texts:
        lines.append(f"| {e} | {len(words[e])} | " + (f"US${costs[e]:.3f}" if e in costs else "—") + " |")

    if len(texts) > 1:
        lines += ["", "## Diferencia entre motores (sin referencia)", "",
                  "WER de cada par. No dice cuál está bien: dice cuánto difieren.", "",
                  "| Par | Diferencia |", "|---|---|"]
        for a, b in itertools.combinations(texts, 2):
            lines.append(f"| {a} vs {b} | {wer(words[a], words[b])[0]:.1%} |")

    if args.ref:
        ref = normalize(args.ref.read_text(encoding="utf-8"))
        ref_nums = numbers(ref)
        lines += ["", f"## Contra la referencia ({len(ref)} palabras, {sum(ref_nums.values())} números)", "",
                  "| Motor | WER | Sustituciones | Borrados | Inserciones | Números bien |", "|---|---|---|---|---|---|"]
        for e in texts:
            w, ops = wer(ref, words[e])
            hits = sum((ref_nums & numbers(words[e])).values())
            lines.append(f"| {e} | **{w:.1%}** | {ops['S']} | {ops['D']} | {ops['I']} | "
                         f"{hits}/{sum(ref_nums.values())} |")
        lines += ["", "Sustitución = palabra cambiada · Borrado = palabra que faltó · "
                      "Inserción = palabra que no estaba (alucinación)."]

    report = "\n".join(lines) + "\n"
    (args.session_dir / "comparacion.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"textos y reporte en {args.session_dir}/")


if __name__ == "__main__":
    main()
