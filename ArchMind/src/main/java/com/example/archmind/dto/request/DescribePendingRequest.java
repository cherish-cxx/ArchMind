package com.example.archmind.dto.request;

import lombok.Data;

/** 内部批量补齐描述的单批入参。 */
@Data
public class DescribePendingRequest {

    private Long projectId;

    /** 每批类数；不传用默认值，服务端钳在 [5, 40] */
    private Integer limit;
}
