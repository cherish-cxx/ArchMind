package com.example.archmind.dto.response;

import lombok.Data;

/**
 * 能力卡片里的成员类：{@code depth} 为从入口出发的最短跳数（入口类为 0）。
 *
 * <p>与 {@link MemberDto} 的区别是可变 DTO —— 索引接口走 Jackson 序列化，
 * 需要 setter 才能按需拼装。</p>
 */
@Data
public class CapabilityMemberDTO {

    private String uid;

    private String name;

    private String role;

    private int depth;
}
