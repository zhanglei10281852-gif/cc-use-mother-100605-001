from experience_registry.core import Record, summarize


if __name__ == "__main__":
    record = Record.create("demo", "v1", "draft", "operator", {"场景": "录入经验条目并生成稳定摘要"})
    print(summarize(record))

