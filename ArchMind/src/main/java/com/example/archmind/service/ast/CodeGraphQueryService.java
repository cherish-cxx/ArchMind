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

    /** 查某个类的上下游关联（类级聚合）。uid = 类的全限定名 */
    ClassRelationsResponse classRelations(Long projectId, String uid);
}
