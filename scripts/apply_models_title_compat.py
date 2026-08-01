from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'control-panel/src/pages/Models.tsx'
text = path.read_text(encoding='utf-8')
old = "                  aria-label={`测试 ${providerName} 连通性`}\n                  aria-busy={testResults[providerName]?.loading ?? false}\n"
new = "                  aria-label={`测试 ${providerName} 连通性`}\n                  title=\"测试连通性\"\n                  aria-busy={testResults[providerName]?.loading ?? false}\n"
if old in text:
    path.write_text(text.replace(old, new, 1), encoding='utf-8')
elif new not in text:
    raise RuntimeError('Models connectivity button pattern missing')
