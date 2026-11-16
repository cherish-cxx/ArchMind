package com.example.archmind.dto.response;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class McpProjectItem {

    private Long projectId;

    private String name;

    private String analysisStatus;

    private Long graphRev;
}
