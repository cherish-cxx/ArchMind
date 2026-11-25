package com.example.archmind.dto.response;

import lombok.Data;

import java.util.List;

/**
 * 索引专用的能力卡片：能力摘要 + 所属领域 + 入口 + 成员类。
 *
 * <p>领域不单独建点 —— 领域文本直接写进卡片；领域命中由检索侧按
 * {@code areaUid/areaName} 聚合。{@code members} 只返回 cap 个，
 * 全量成员数在 {@code memberCount}，超出时 {@code membersTruncated} 为 true。</p>
 */
@Data
public class CapabilityCardDTO {

    private String uid;

    private String name;

    private String description;

    private String areaUid;

    private String areaName;

    private String areaDescription;

    private String entryUid;

    private String entryKind;

    private int memberCount;

    private List<CapabilityMemberDTO> members = List.of();

    private boolean membersTruncated;
}
