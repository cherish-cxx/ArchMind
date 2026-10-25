package com.example.archmind.dto.request;

import lombok.Data;

import java.util.List;

/**
 * 按需生成类描述。uids 由前端按「画布上有、但没有描述」的节点收集。
 */
@Data
public class ClassDescriptionRequest {

    private List<String> uids;

    /**
     * true 时放宽类型限制（接口 / 抽象类 / 枚举也生成）。
     * 只给「画布中心节点」用 —— 用户正看着它，空卡片比多一次调用更糟。
     * 已有描述的仍然跳过。
     */
    private boolean force;
}
