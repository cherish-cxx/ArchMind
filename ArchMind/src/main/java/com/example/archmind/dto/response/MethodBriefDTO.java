package com.example.archmind.dto.response;

import lombok.Data;

/**
 * 卡片里的一个方法摘要。
 *
 * <p>只给索引/检索用，不带源码；{@code signature} 已含参数列表，
 * 与 {@code owner#signature} 的 method uid 口径一致。</p>
 */
@Data
public class MethodBriefDTO {

    private String name;

    /** 例如 {@code createOrder(OrderRequest)}；也是 Method uid 的后半段 */
    private String signature;

    private String visibility;

    private Boolean isStatic;

    private Boolean isConstructor;
}
