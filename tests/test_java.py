"""Java support: public symbols (types, Spring/JAX-RS routes, JPA models, configuration) and Maven/Gradle repo facts."""
from multisync.repofacts import conflicts, deterministic_facts, deterministic_signals, parse_gradle, parse_maven
from multisync.symbols import diff_public_symbols, extract_public_symbols


def names(file, text):
    return sorted(f"{s['kind']}:{s['name']}" for s in extract_public_symbols(file, text))


CONTROLLER = '''package a;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping("/api/v1/transactions")
public class TransactionController {
    public static final String ID = "/{id}";
    @PostMapping
    public X create() {}
    @GetMapping(ID)
    public X find() {}
    @GetMapping(value = "/{id}/tickets", produces = "application/json")
    public X tickets() {}
    @RequestMapping(path = {"/a", "/b"}, method = RequestMethod.PUT)
    public X put() {}
    // @DeleteMapping("/commented-out")
    @DeleteMapping("/{id}")
    void hidden() {}
}
'''


def test_java_public_types_only_and_records_enums_interfaces():
    t = ("public class Svc {}\nclass Hidden {}\npublic interface Repo { Foo find(); }\npublic enum Status { A, B }\n"
         "public record PageResponse<T>(int page, List<T> items) {}\n  public static final class Nested {}\npublic abstract class Base {}\n")
    assert names("a/Svc.java", t) == sorted(["export:Svc", "export:Repo", "export:Status", "export:PageResponse", "export:Nested", "export:Base"])


def test_spring_routes_combine_class_prefix_constants_and_skip_comments():
    assert names("TransactionController.java", CONTROLLER) == sorted([
        "export:TransactionController", "route:POST /api/v1/transactions", "route:GET /api/v1/transactions/{id}", "route:GET /api/v1/transactions/{id}/tickets",
        "route:PUT /api/v1/transactions/a", "route:PUT /api/v1/transactions/b", "route:DELETE /api/v1/transactions/{id}"])


def test_spring_class_without_prefix_and_request_mapping_without_method():
    t = 'public class H {\n  @GetMapping("/marketplace")\n  String a() {}\n  @RequestMapping("/any")\n  String b() {}\n  @GetMapping\n  String c() {}\n}'
    assert names("H.java", t) == ["export:H", "route:ANY /any", "route:GET /", "route:GET /marketplace"]


def test_jaxrs_paths_and_verbs():
    t = '@Path("/orders")\npublic class OrderResource {\n  @GET\n  @Path("/{id}")\n  public Order get() {}\n  @POST\n  public Order create() {}\n}'
    assert names("OrderResource.java", t) == ["export:OrderResource", "route:GET /orders/{id}", "route:POST /orders"]


def test_jpa_entities_are_models_with_fields_in_the_signature():
    t = '@Entity\n@Table(name = "tickets")\npublic class Ticket {\n  private String seat;\n}\n'
    assert names("Ticket.java", t) == ["model:Ticket", "model:tickets"]
    after = t.replace("String seat;", "String seat;\n  private String row;")
    assert diff_public_symbols(f"### FILE: T.java\n{t}", f"### FILE: T.java\n{after}")["changed"] == ["model:Ticket"]


def test_java_config_keys_env_vars_and_spring_property_files():
    t = 'class C {\n @Value("${fidavia.backend.base-url:http://x}") String u;\n String e = System.getenv("DB_USERNAME");\n}\n@ConfigurationProperties(prefix = "fidavia.ui")\nclass P {}'
    assert names("C.java", t) == ["config:DB_USERNAME", "config:fidavia.backend.base-url", "config:fidavia.ui"]
    props = "# c\nspring.datasource.url=jdbc:x\nspring.datasource.password=${DB_PASSWORD}\nfidavia.cors.allowed-origins=${FIDAVIA_CORS_ORIGINS:}\n"
    assert names("src/main/resources/application.properties", props) == sorted(
        ["config:spring.datasource.url", "config:spring.datasource.password", "config:fidavia.cors.allowed-origins", "config:DB_PASSWORD", "config:FIDAVIA_CORS_ORIGINS"])
    yml = "server:\n  port: 8090\nspring:\n  datasource:\n    url: jdbc:x\n  profiles:\n    active: ${PROFILE:dev}\n"
    assert names("application-dev.yml", yml) == sorted(["config:server.port", "config:spring.datasource.url", "config:spring.profiles.active", "config:PROFILE"])
    assert names("other.properties", "a.b=c") == []


def test_java_route_added_or_removed_is_a_public_change():
    before = "### FILE: A.java\n" + CONTROLLER
    after = before.replace('@DeleteMapping("/{id}")', '@DeleteMapping("/{id}/gone")')
    d = diff_public_symbols(before, after)
    assert d["added"] == ["route:DELETE /api/v1/transactions/{id}/gone"] and d["removed"] == ["route:DELETE /api/v1/transactions/{id}"]


POM = '''<project><parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>4.1.1</version></parent>
<groupId>mx.fidavia</groupId><artifactId>fidavia</artifactId><version>0.0.1</version>
<properties><java.version>25</java.version></properties>
<dependencies><dependency><artifactId>spring-boot-starter-webmvc</artifactId></dependency>
<dependency><artifactId>spring-boot-starter-data-jpa</artifactId></dependency><dependency><artifactId>postgresql</artifactId></dependency></dependencies>
<build><plugins><plugin><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build></project>'''


def test_maven_facts_group_artifact_java_boot_and_dependencies():
    info = parse_maven(POM)
    assert (info["group"], info["artifact"], info["java"], info["boot"]) == ("mx.fidavia", "fidavia", "25", "4.1.1")
    assert info["deps"] == ["Spring MVC", "Spring Data JPA", "PostgreSQL"]
    facts = deterministic_facts("o/fidavia", ["pom.xml", "src/main/java/A.java"], {"pom.xml": POM, "src/main/java/A.java": "public class A {}\n"}.get)
    text = " ".join(f["fact"] for f in facts)
    assert "mx.fidavia:fidavia targeting Java 25, on Spring Boot 4.1.1" in text and "mainly written in Java" in text


def test_gradle_facts():
    g = "plugins { id 'org.springframework.boot' version '3.3.1' }\ngroup = 'com.acme'\nversion = '1.0'\njava { toolchain { languageVersion = JavaLanguageVersion.of(21) } }\ndependencies { implementation 'org.springframework.boot:spring-boot-starter-web'\n implementation 'org.flywaydb:flyway-core:10.0' }"
    info = parse_gradle(g)
    assert (info["group"], info["java"], info["boot"]) == ("com.acme", "21", "3.3.1") and info["deps"] == ["Spring MVC", "Flyway migrations"]


def test_readme_claim_about_java_or_spring_boot_version_is_checked_against_the_build():
    sig = deterministic_signals(["pom.xml"], {"pom.xml": POM}.get)
    assert sig["java"] == "25" and sig["frameworks"]["Spring Boot"] == "4"
    mk = lambda t: {"fact": t, "evidence": t}
    assert conflicts(mk("Requires JDK 17 to build"), sig).startswith("the README mentions Java 17")
    assert conflicts(mk("Built on Spring Boot 3.2"), sig).startswith("the README mentions Spring Boot 3")
    assert conflicts(mk("Requires Java 25 and Spring Boot 4.1.1"), sig) is None
