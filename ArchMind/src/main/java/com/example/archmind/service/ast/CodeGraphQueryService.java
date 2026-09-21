package com.example.archmind.service.ast;

import com.example.archmind.dto.response.ClassNodeDTO;
import com.example.archmind.dto.response.ClassRelationsResponse;

import java.util.List;

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
}
