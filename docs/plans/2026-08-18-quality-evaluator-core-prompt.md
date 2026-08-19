# Quality Evaluator 公共 Prompt 实现计划

## 目标

实现一份 XML 分区的公共系统 Prompt。运行时只替换研究领域、当前研究类型 Rubric 和 Pydantic JSON Schema，不为每种研究类型复制完整 Prompt。

## Task 1：锁定 Prompt 契约

**文件：**

- 新增：`tests/test_quality_evaluator_prompt.py`
- 新增：`skills/quality_evaluator/prompts/core_prompt.md`

1. 先编写测试，要求模板包含唯一根标签、角色、任务、领域、规则、输出结构、约束和提示区块。
2. 要求模板恰好包含 `__STUDY_DOMAIN__`、`__RUBRIC_RULES__` 和 `__OUTPUT_SCHEMA__` 三个运行时替换标记。
3. 运行 `pytest tests/test_quality_evaluator_prompt.py -q`，确认测试因模板不存在而失败。
4. 创建最小模板并再次运行测试，确认通过。

## Task 2：验证 Skill 资源

1. 运行 `python -X utf8 D:\CodexData\.codex\skills\.system\skill-creator\scripts\quick_validate.py skills/quality_evaluator`。
2. 运行 `git diff --check -- skills/quality_evaluator/prompts/core_prompt.md tests/test_quality_evaluator_prompt.py`。
3. 确认没有修改运行代码、Schema 或研究类型 Rubric。

