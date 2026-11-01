# R4 语义入口评测报告

- 生成时间：2026-10-31T05:13:55.722447+00:00
- 项目：`2115334472151040002`，rev `1`，索引点数 104
- 数据集：`scripts\eval\retrieval_dataset.archmind.json`
- 候选数：top-10

## 总览

| 检索方式 | recall@1 | recall@3 | recall@5 | recall@10 | MRR@10 | hit@5 | P50 | P95 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| hybrid | 42.5% | 69.2% | 79.6% | 94.2% | 0.793 | 95.0% | 447.2ms | 562.7ms |
| dense | 44.2% | 72.5% | 80.0% | 92.9% | 0.819 | 97.5% | 447.2ms | 562.7ms |
| sparse | 41.2% | 71.7% | 78.3% | 89.2% | 0.773 | 95.0% | 447.2ms | 562.7ms |
| dbsf | 42.5% | 73.8% | 82.1% | 91.7% | 0.800 | 97.5% | 447.2ms | 562.7ms |

> 锚点级 recall@5：77.1%（所有锚点里落在前 5 的比例）
> 主锚点 hit@5：95.0%（每条问题第一个锚点落在前 5 的比例）
> 检索层延迟为生产 hybrid 路径的 embed + Qdrant 总耗时；融合对照共用同一次 embed。

## 分类

| 分类 | 题数 | recall@5 | MRR@10 |
| --- | --- | --- | --- |
| analysis | 7 | 81.0% | 0.929 |
| auth | 8 | 75.0% | 0.556 |
| graph | 2 | 25.0% | 0.312 |
| misc | 4 | 100.0% | 1.000 |
| overview | 6 | 83.3% | 0.917 |
| project | 4 | 91.7% | 0.833 |
| task | 6 | 83.3% | 0.889 |
| upload | 3 | 66.7% | 0.667 |

## 未命中前 5 的问题

- `Q01` 用户注册的时候密码是怎么加密保存的？（SecurityConfig=5, UserServiceImpl=6）
- `Q02` 登录成功后返回给前端的凭证里包含哪些信息？（LoginResponse=1, AuthController=None）
- `Q04` 每次请求进来时是怎么识别当前用户的？（JwtAuthenticationFilter=6, SecurityUser=None）
- `Q09` 上传的项目压缩包是怎么安全解压的？（ZipExtractServiceImpl=2, UploadServiceImpl=6）
- `Q10` 上传源码后文件记录是怎么写进数据库的？（UploadServiceImpl=2, FileScannerServiceImpl=7）
- `Q13` 创建项目的接口和请求参数是什么？（ProjectController=2, ProjectCreateRequest=1, ProjectServiceImpl=None）
- `Q19` Java 源码是怎么解析成类、字段和方法的？（AstParserServiceImpl=1, ParsedClass=8, ParsedMethod=2）
- `Q20` 方法之间的调用关系是怎么推断出来的？（RelationResolver=1, TypeResolver=7）
- `Q21` 解析结果是怎么批量写进图数据库的？（CodeGraphNeo4jWriter=2, CodeGraphServiceImpl=None）
- `Q22` 怎么查一个类被谁调用、又依赖了谁？（CodeGraphQueryServiceImpl=8）
- `Q24` 一个源文件解析后用什么对象表示？（ParsedFile=1, ParsedClass=8）
- `Q25` 任务状态有哪些取值，怎么判断任务已经结束？（TaskStatus=1, Task=8）
- `Q27` 分析任务实体里包含哪些字段？（Task=1, TaskResponse=None）
- `Q34` 批量补类描述的接口返回什么结构？（ClassDescriptionBatch=1, ClassDescriptionServiceImpl=7）
- `Q36` 项目基本信息返回给前端是什么结构？（ProjectResponse=1, Project=6）

## 负样例（域外问题）

| id | 问题 | top1 分 |
| --- | --- | --- |
| N01 | 帮我写一个快速排序算法 | 0.75 |
| N02 | 今天北京的天气怎么样？ | 0.75 |
| N03 | 这个项目用的是 Vue 还是 React？ | 0.8333 |
| N04 | 如何把服务部署到 Kubernetes？ | 0.5 |
| N05 | 推荐几条 Java 后端学习路线 | 0.5714 |
| N06 | 解释一下分布式事务的两阶段提交 | 0.8333 |
| N07 | 这个项目的开源许可证是什么？ | 0.7 |
| N08 | 帮我优化一下 MySQL 的慢查询 | 0.6667 |

## 结论

- 严格锚点 recall@5 = 79.6%（验收线 80%） —— FAIL
- 主锚点 hit@5 = 95.0%；问题级 hit@5 = 95.0%
- 范围越界：0 条；空结果：0 条
