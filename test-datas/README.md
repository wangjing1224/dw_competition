# 本地 Mock 演示数据

该目录用于在不调用真实 Chat、Embedding 和 Rerank 服务的情况下验证完整代码流程。

```text
test-datas/
├── 招标文件/
├── 投标方资料/
└── 投标文件测试集/
```

预期结果：

- `01-红外热成像仪投标文件.md` 与项目匹配，进入规则审核。
- 像素要求有投标响应，也能在技术参数资料中找到支撑。
- 投标总价明确标记为未填写，形成风险项。
- `02-机械加工设备投标文件.md` 与项目不匹配，输出 `not_applicable`。
- 最终生成一份任务级 JSON 和一份适用文件的 Word 报告。

运行：

```bash
uv run pytest tests/e2e/test_mock_dataset.py -v -s
```

该测试使用确定性 Mock 模型，不读取 `.env` 中的模型 Key，也不会产生外部模型费用。
