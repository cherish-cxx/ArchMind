package com.example.archmind.config;

import com.example.archmind.common.handler.AccessDeniedHandlerImpl;
import com.example.archmind.common.handler.AuthenticationEntryPointImpl;
import com.example.archmind.common.security.InternalTokenFilter;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.authentication.AuthenticationManager;
import org.springframework.security.config.annotation.authentication.configuration.AuthenticationConfiguration;
import org.springframework.security.config.annotation.method.configuration.EnableMethodSecurity;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;
import org.springframework.web.cors.CorsConfiguration;
import org.springframework.web.cors.CorsConfigurationSource;
import org.springframework.web.cors.UrlBasedCorsConfigurationSource;
import tools.jackson.databind.ObjectMapper;

import java.util.Arrays;

@Configuration
@EnableWebSecurity
@EnableMethodSecurity  // 启用方法级权限控制（@PreAuthorize）
public class SecurityConfig {

    /** 与 Agent 服务共享的内部密钥；生产环境用 INTERNAL_TOKEN 环境变量覆盖 */
    @Value("${internal.token}")
    private String internalToken;

    /**
     * 密码编码器
     * BCrypt 加密，自动加盐
     */
    @Bean
    public PasswordEncoder passwordEncoder() {
        return new BCryptPasswordEncoder();
    }

    /**
     * 认证管理器
     */
    @Bean
    public AuthenticationManager authenticationManager(AuthenticationConfiguration config) throws Exception {
        return config.getAuthenticationManager();
    }

    /**
     * 内部接口令牌过滤器（只作用于 {@code /internal/**}）。
     * 不标 {@code @Component} —— 显式建成 Bean 挂进安全链，避免被 Servlet 容器重复注册一次。
     */
    @Bean
    public InternalTokenFilter internalTokenFilter(ObjectMapper objectMapper) {
        return new InternalTokenFilter(internalToken, objectMapper);
    }

    /**
     * 安全过滤器链
     */
    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http,
                                           JwtAuthenticationFilter jwtAuthenticationFilter,
                                           InternalTokenFilter internalTokenFilter,
                                           AuthenticationEntryPointImpl authenticationEntryPoint,
                                           AccessDeniedHandlerImpl accessDeniedHandler) throws Exception {
        http
                // 1. 关闭 CSRF（JWT 无状态，不需要 CSRF）
                .csrf(csrf -> csrf.disable())

                // 2. CORS 配置
                .cors(cors -> cors.configurationSource(corsConfigurationSource()))

                // 3. 会话管理：无状态
                .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))

                // 4. 请求授权配置
                .authorizeHttpRequests(auth -> auth
                        // 白名单路径（无需认证）
                        .requestMatchers(
                                "/api/auth/login",
                                "/api/auth/register",
                                "/api/auth/refresh",
                                "/api/auth/captcha",
                                "/actuator/health",
                                "/swagger-ui/**",
                                "/v3/api-docs/**",
                                "/doc.html",
                                "/webjars/**",
                                // 内部取数接口：不放行会被 anyRequest().authenticated() 拦成 401，
                                // 而调用方（Agent 服务）按设计拿不到 JWT。这里放行后由
                                // InternalTokenFilter 用共享密钥把关。
                                "/internal/**"
                        ).permitAll()
                        // 需要认证的路径
                        .requestMatchers("/api/**").authenticated()
                        // 其他请求需要认证
                        .anyRequest().authenticated()
                )

                // 5. 异常处理
                .exceptionHandling(ex -> ex
                        .authenticationEntryPoint(authenticationEntryPoint)   // 未认证
                        .accessDeniedHandler(accessDeniedHandler)             // 无权限
                )

                // 6. 添加 JWT 过滤器（在 UsernamePasswordAuthenticationFilter 之前）
                .addFilterBefore(jwtAuthenticationFilter, UsernamePasswordAuthenticationFilter.class)

                // 7. 内部接口令牌过滤器：只对 /internal/** 生效，其余路径直接放行
                .addFilterBefore(internalTokenFilter, UsernamePasswordAuthenticationFilter.class);

        return http.build();
    }

    /**
     * CORS 跨域配置
     */
    @Bean
    public CorsConfigurationSource corsConfigurationSource() {
        CorsConfiguration configuration = new CorsConfiguration();
        // 允许的来源：开发期放开 localhost/127.0.0.1 任意端口（5173/5174/3000/8080 等均覆盖）
        configuration.setAllowedOriginPatterns(Arrays.asList(
                "http://localhost:*",
                "http://127.0.0.1:*"
        ));
        // 允许的请求方法
        configuration.setAllowedMethods(Arrays.asList("GET", "POST", "PUT", "DELETE", "OPTIONS"));
        // 允许的请求头
        configuration.setAllowedHeaders(Arrays.asList("*"));
        // 允许携带凭证（Cookie）
        configuration.setAllowCredentials(true);
        // 预检请求的缓存时间
        configuration.setMaxAge(3600L);

        UrlBasedCorsConfigurationSource source = new UrlBasedCorsConfigurationSource();
        source.registerCorsConfiguration("/**", configuration);
        return source;
    }
}