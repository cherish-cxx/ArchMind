package com.example.archmind.dto.response;

import lombok.Data;

import java.util.ArrayList;
import java.util.List;

/** Python 侧会话索引的包体：{@code {"conversations": [...]}}。 */
@Data
public class ConversationListDTO {

    private List<ConversationSummaryDTO> conversations = new ArrayList<>();
}
