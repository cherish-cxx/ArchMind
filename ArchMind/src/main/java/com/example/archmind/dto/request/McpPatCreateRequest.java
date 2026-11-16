package com.example.archmind.dto.request;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class McpPatCreateRequest {

    private String name;

    /** 1..365；为空默认 90 天 */
    private Integer expiresInDays;
}
