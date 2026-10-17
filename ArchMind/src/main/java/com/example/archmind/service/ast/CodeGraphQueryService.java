package com.example.archmind.service.ast;

import com.example.archmind.dto.response.ClassNodeDTO;
import com.example.archmind.dto.response.ClassRelationsResponse;

import java.util.Collection;
import java.util.List;
import java.util.Set;

/**
 * 代码图谱查询服务：从 Neo4j 读「类清单」与「点类看关联」。
 * 与 CodeGraphService（写图）相对，只读不写。
 */
public interface CodeGraphQueryService {

    /** 某项目全部类节点，按包名→类名排序，供前端「代码探索」入口 */
    List<ClassNodeDTO> listClasses(Long projectId);

    /**
     * 同上，但只取前 {@code limit} 个。
     * 供内部工具用 —— 全量返回会把上千个节点塞进信封，Neo4j 侧也要白白序列化一遍。
     */
    List<ClassNodeDTO> listClasses(Long projectId, int limit);

    /** 查某个类的上下游关联（类级聚合）。uid = 类的全限定名 */
    ClassRelationsResponse classRelations(Long projectId, String uid);

    /**
     * 同上，但只查指定方向。
     * {@code direction} 为 UP 时，返回体的 {@code downstream} 为 null（表示「没查」而非「没有」）。
     */
    ClassRelationsResponse classRelations(Long projectId, String uid, RelationDirection direction);

    /**
     * 批量查这些 uid 里，**哪些确实是这个项目的类节点**。
     *
     * <p>供 P4.3 网关做 uiCommands 弱校验：Python 返回的出图命令可能指向
     * 不存在的节点（图被重建过、或 Python 有 bug），Java 这层是第二道防线。</p>
     *
     * <p><b>必须批量</b> —— 一条响应可能带 N 条命令，逐条查 Neo4j 就是 N 次往返。</p>
     *
     * @return 存在的那些 uid（不存在的不在结果里，调用方做差集）
     */
    Set<String> existingClassUids(Long projectId, Collection<String> uids);
}
