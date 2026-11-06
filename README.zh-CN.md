# CityNexus｜全部研发成果总门户

**面向外教的英文首页：[English README](README.md)** · **[英文总门户](docs/PROJECT_PORTAL.md)**

我们在 2026 年 9 月至 11 月围绕「能陪伴旅行全程的 Agent」开展了多条开发线。课程 Part 2 正式 PDF 已单独交到 Moodle。这里展示的是跨仓库、跨分支的**真实项目源码、阶段性结果、截图、实验与软件工程材料**，不是一个已经正式上线的成品应用。

## 四条主线

| 主线 | 实际工作 | 最值得打开的资料 |
| --- | --- | --- |
| 主产品与软件工程基础 | React、TypeScript、FastAPI、地图、旅行计划、酒店预算、地点来源、需求与架构 | [功能源码索引](docs/FEATURE_ATLAS.md) · [英文首页截图](docs/VISUAL_GALLERY.md) |
| Codex · Revolution Lab | DeepSeek/Hermes 工具与 Agent、Guardian、主动恢复、多人协调、真实路线、双站 Pocket Walk | [10 个实验案例](docs/EXPERIMENT_RESULTS.md) · [103 份结果文档目录](docs/RESEARCH_ARCHIVE_INDEX.md) |
| Qoder · Revolution V3 | 真实用户操作流程、中文意图、Agent 工具时间线、地图、主动规划、虚拟时间与虚拟定位的课堂演示 | [六张 V3 真实截图与说明](docs/CLASSROOM_AND_V3.md) · [原始 10 月 29 日研发分支](https://github.com/Canshu-King/CityNexus/tree/rescue/v3-final-departure-20261022) |
| 课程文档与项目管理 | 138 条候选需求、18 个核心领域实体、13 个主要用例、Visio、Project、甘特图、WBS、架构与追踪矩阵 | [课程材料](teacher-materials/README.md) · [研发时间线](docs/DEVELOPMENT_TIMELINE.md) |

## 五个现已公开的 GitHub 仓库

- [CityNexus：原始主仓库](https://github.com/Canshu-King/CityNexus)，包含历史分支、真实提交记录、Qoder V3、Codex Revolution Lab、11 月课程工作等。**主仓库默认分支停留在 9 月，不能代表最新产品。**
- [CityNexus-Coursework-2026：当前成果门户](https://github.com/Canshu-King/CityNexus-Coursework-2026)。
- [CityNexus-Workspace：课程参考、早期设计与交接](https://github.com/Canshu-King/CityNexus-Workspace)。
- [CityNexus-Lab-Archive：DSH/WorkBuddy 和实验归档](https://github.com/Canshu-King/CityNexus-Lab-Archive)。
- [CityNexus-History：V14/V15 早期开发快照](https://github.com/Canshu-King/CityNexus-History)。

**[查看分支详细关系、重要 SHA 与日期](docs/DEVELOPMENT_STREAMS.md)**

## 这几个月真正值得展示的例子

1. **重庆 Agent 自动调整计划：** 外部模型实际调用地点搜索工具，第一次方案因为营业时间冲突被拒绝，随后在本地 Journey 中调整策略。
2. **奈良步行路线：** Valhalla 真实路线供应商返回一段 **22 分钟**的路线事实，而不是模型凭空估算。
3. **多人旅行与恢复：** 成员独立确认、权限和会合恢复在隔离环境中有真实后端与浏览器测试；不能把自报进度当作物理到场。
4. **苏州两天四站课堂模拟：** 同一隔离模拟运行记录四站完成，期间有真实外部模型和路线调用；**两天、时间、定位和到站进度是模拟的**，Agent 结果包含部分失败。
5. **V3 真人交互：** Chrome 390/430 宽屏从空白入口进入规划并到达 ACTIVE，保留了截图、提交和未完成项；用户本人平板正式验收仍未宣称通过。

这些案例都有[原始结果报告链接](docs/PROJECT_PORTAL.md)，不是凭代码文件名编造的成功案例。

## 给老师的建议阅读顺序

**[完整英文总览](docs/PROJECT_PORTAL.md) → [六张最新 V3 截图](docs/CLASSROOM_AND_V3.md) → [所有实际功能源码](docs/FEATURE_ATLAS.md) → [具体实验结果](docs/EXPERIMENT_RESULTS.md) → [软件工程材料](teacher-materials/README.md)**。

公开仓库现在保留几个月的真实研发记录，但**不同分支没有被声称已经全部融合成统一、生产可用的系统**。历史测试、课件示例、模拟定位、真实供应商、外部 Agent 调用的证据等级均需分别阅读。外教课堂现场没有成功展示全系统，这一点也不会掩盖。
