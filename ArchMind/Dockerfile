# 多阶段构建：镜像里从源码编译，服务器只需 git pull 后重建
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /build

# 国内网络走阿里云镜像；只镜像 central，spring-snapshots 仍走原仓库
# （parent 是 4.1.1-SNAPSHOT，必须能访问 repo.spring.io）
ARG MAVEN_MIRROR=https://maven.aliyun.com/repository/public
RUN printf '%s\n' \
    '<settings>' \
    '  <mirrors>' \
    '    <mirror>' \
    '      <id>aliyun-central</id>' \
    '      <mirrorOf>central</mirrorOf>' \
    '      <url>'"$MAVEN_MIRROR"'</url>' \
    '    </mirror>' \
    '  </mirrors>' \
    '</settings>' > /build/settings.xml

COPY pom.xml ./
COPY src ./src

# 依赖缓存挂在 /root/.m2，代码改动不会重复下载整个依赖树
RUN --mount=type=cache,target=/root/.m2 \
    mvn -B -s /build/settings.xml -DskipTests package

FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /build/target/ArchMind-0.0.1-SNAPSHOT.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
