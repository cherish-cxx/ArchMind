package com.example.archmind.controller;

import com.example.archmind.common.util.CheckProjectUtil;
import com.example.archmind.dto.response.McpPatCreatedResponse;
import com.example.archmind.dto.response.McpPatView;
import com.example.archmind.service.McpPatService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.util.List;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@ExtendWith(MockitoExtension.class)
class McpPatControllerTest {

    @Mock
    private McpPatService service;
    @Mock
    private CheckProjectUtil checkProjectUtil;

    private MockMvc mvc;

    @BeforeEach
    void setUp() {
        mvc = MockMvcBuilders.standaloneSetup(
                new McpPatController(service, checkProjectUtil)).build();
    }

    @Test
    void createUsesCurrentUser() throws Exception {
        when(checkProjectUtil.getCurrentUserId()).thenReturn(7L);
        when(service.create(eq(7L), any())).thenReturn(
                McpPatCreatedResponse.builder().id(1L).token("am_pat_x").build());

        mvc.perform(post("/api/mcp/tokens")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"name\":\"claude-code\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.token").value("am_pat_x"));

        verify(service).create(eq(7L), any());
    }

    @Test
    void listNeverReturnsPlaintextToken() throws Exception {
        when(checkProjectUtil.getCurrentUserId()).thenReturn(7L);
        when(service.list(7L)).thenReturn(List.of(
                McpPatView.builder().id(1L).prefix("am_pat_abc").build()));

        mvc.perform(get("/api/mcp/tokens"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data[0].token").doesNotExist());
    }

    @Test
    void revokeUsesCurrentUserAndPatId() throws Exception {
        when(checkProjectUtil.getCurrentUserId()).thenReturn(7L);

        mvc.perform(delete("/api/mcp/tokens/42"))
                .andExpect(status().isOk());

        verify(service).revoke(7L, 42L);
    }
}
