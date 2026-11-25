package com.example.archmind.dto.response;

import lombok.Data;

import java.util.List;

/**
 * 能力卡片的一页。分页语义与 {@link ClassCardPageResponse} 一致：
 * keyset 游标 + rev；调用方发现 rev 变化要中止重来。
 */
@Data
public class CapabilityCardPageResponse {

    private long rev;

    private List<CapabilityCardDTO> items = List.of();

    /** 下一页的 afterUid；null = 没有下一页 */
    private String nextCursor;
}
