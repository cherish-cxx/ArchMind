反馈问题：
对重点文件的分析太肤浅
只做到了上下游，调用被调用，依赖内容，核心的的内容实现逻辑看不到
主要实现的方法看不到没有解析
前端分析后没有刷新也看不到轮询效果，不刷新看到是否完成解析
同一个项目会被多次创建，没有锁限制
领域核心没有重点体现，太臃肿了


# agent设计第一版

有限制的范围内，对模块内容进行引导探索


```
我已经定位到订单模块。

你可以从以下方向开始探索：

[模块职责]

[核心代码]

[业务能力]

[模块依赖]

[业务流程]

或者直接提问。
```


V1系统框架
```
                        用户
                         │
                         ↓
                  ┌─────────────┐
                  │ Agent API   │
                  └──────┬──────┘
                         ↓
                  ┌─────────────┐
                  │ Agent Core  │
                  ├─────────────┤
                  │ Context     │
                  │ Intent      │
                  │ Planner     │
                  │ Tool Call   │
                  │ Reasoning   │
                  └──────┬──────┘
                         │
             ┌───────────┼───────────┐
             ↓           ↓           ↓
         Neo4j Tools  MySQL Tools  Analysis Tools
             │           │           │
             ↓           ↓           ↓
          Neo4j        MySQL       Java Analysis
                         │
                         ↓
                      LLM
                         │
                         ↓
                  Agent Response
                         │
             ┌───────────┴───────────┐
             ↓                       ↓
        Message                    UI Commands
             │                       │
             ↓                       ↓
         Chat UI                Graph UI
                                     │
                                     ↓
                              Project Knowledge
```

流程线路：
```
用户
 ↓
Agent
 ↓
Tool
 ↓
Neo4j
 ↓
结构化事实
 ↓
LLM
 ↓
解释 + UI Command
 ↓
前端图谱变化
 ↓
用户继续探索
```

## 需要确定的内容
1. agent Context
	- agent Context是agent当前这次探索所处的上下文状态
	作用：
	- 维持用户内容的探索
	包含：
```
AgentContext
│
├── project
│   └── projectId
│
├── target
│   ├── type
│   ├── id
│   └── name
│
├── currentFocus
│   ├── type
│   ├── id
│   └── name
│
├── visibleNodes
│
├── visibleRelations
│
├── explorationHistory
│
└── conversation
```

### project

```
{
  "projectId": 1001
}
```

这是最基本的。

Agent 必须知道：

> 我正在分析哪个项目。

### target

用户最开始选择：

`订单模块`

那么：

```
{
  "target": {
    "type": "BUSINESS_DOMAIN",
    "id": 2001,
    "name": "订单模块"
  }
}
```

这就是：

> **本次探索的根目标。**

### currentFocus

例如：

`订单模块 ↓ OrderService`

用户点击 OrderService。

那么：

```
{
  "currentFocus": {
    "type": "CLASS",
    "id": 3001,
    "name": "OrderService"
  }
}
```

后面用户：

> “它调用了谁？”

Agent 就知道：

```
它 = OrderService
```


### visibleNodes

Agent控制可视化：
需要观察记录屏幕上展示的节点，同步到上下文中


例如：
```
visibleNodes:

OrderController
OrderService
OrderMapper
InventoryService
```

关系：
```
OrderController → OrderService
OrderService → InventoryService
OrderService → OrderMapper
```

### explorationHistory
记录：用户已经探索了什么，当用户需要回到前一个流程需要知道前一个流程的内容

Context中不要放：

```
整个项目代码
整个Neo4j
所有节点
所有历史对话
所有LLM结果
```
会导致Context内容越来越大，幻觉增大，成本增大。

正确方式

```
Context
    ↓
记录“状态”和“引用”
    ↓
需要详细数据
    ↓
Tool查询
```
Context存状态，不存完整的知识。


2. Tool Catalog
- 工具清单，agent能够使用哪些工具，有哪些工具提供使用

- 解决了什么问题：
  能够通过调用提前设计好的工具去获取实际资源：
  Neo4j，Mysql等等数据资源
  是LLM与真实项目资源的接口

- Tool如何设计：
  设计原则：
	不要按照数据库表设计Tool，应该按照agent需要完成什么任务设计

- Tool一定要结构化输入输出
- 输入：
```
{
  "nodeType": "CLASS",
  "nodeId": 3001,
  "maxDepth": 1,
  "limit": 20
}
```

- 输出

```
{
  "source": {
    "id": 3001,
    "name": "OrderService"
  },
  "nodes": [
    {
      "id": 4001,
      "type": "CLASS",
      "name": "InventoryService"
    }
  ],
  "relations": [
    {
      "source": 3001,
      "target": 4001,
      "type": "CALLS"
    }
  ]
}
```


3. UI Command Schema（用户界面命令模式）

- 规定agent可以如何控制前端可视化界面
  LLM负责产生意图，前端负责执行命令
- 
例如：

```
{
  "action": "FOCUS_NODE",
  "target": {
    "type": "CLASS",
    "id": "3001"
  }
}
```

前端：

`收到 FOCUS_NODE ↓ Graph Store ↓ focusNode(3001)`

---

 UI Command 暂时先做：
 
```
FOCUS_NODE
ADD_NODE
REMOVE_NODE
EXPAND_NODE
COLLAPSE_NODE
HIGHLIGHT_NODE
HIGHLIGHT_RELATION
SHOW_PATH
CLEAR_VIEW
```

一个较好的流程：
```
Agent
 ↓
Tool
 ↓
OrderService真实存在
 ↓
Tool返回node
 ↓
Agent决定ADD_NODE
 ↓
UI
```

4. Agent边界

 - 规定agent可以做什么，不允许做什么

```
允许：

✓ 查询
✓ 分析
✓ 解释
✓ 推荐下一步探索
✓ 控制可视化
```

第一阶段的目的是负责用户理解已有项目

- 限制知识来源：

需要根据Tool查询结果进行返回，得到结果。如果没有则按Tool返回的内容进行Response


- 限制Tool调用的数量，调用的深度等等

防止出现：
```
用户一句话
 ↓
Agent
 ↓
Tool
 ↓
Tool
 ↓
Tool
 ↓
Tool
 ↓
Tool
 ↓
Tool
...
```


# 设计根据

```
① 用户到底要完成什么任务？
             ↓
② 用户会怎么操作？
             ↓
③ Agent需要知道什么？
             ↓
④ Agent需要查询什么？
             ↓
⑤ Agent可以调用什么？
             ↓
⑥ Agent需要改变什么UI？
             ↓
⑦ Agent允许做到什么程度？
```
先成为用户，设计用户任务，再反推agent