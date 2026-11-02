// SPDX-License-Identifier: AGPL-3.0-only
// Shared macOS/Linux adapter for Bambu Studio's unmodified networking plugin.
// The public ABI is pinned to Studio v02.08.02.61 / plugin 02.08.02.x.
// No GUI or app-binary modification. The launcher loads a process-local
// executable-path shim into this helper, not into Bambu Studio.
#include <nlohmann/json.hpp>
#include <iostream>
#include <iterator>
using json = nlohmann::json;
#include <dlfcn.h>
#include <unistd.h>
#include <fcntl.h>
#include <atomic>
#include <chrono>
#include <thread>
#include <mutex>
#include "vendor/bambu_networking.hpp"

static int result_fd=-1;
static std::mutex output_mutex;
static void emit(const json &value) {
    const std::string data=value.dump()+"\n";
    std::lock_guard<std::mutex> lock(output_mutex);
    const char *p=data.data(); size_t left=data.size();
    while(left){ssize_t n=write(result_fd,p,left);if(n<=0)_exit(3);p+=n;left-=n;}
}
template<class T> static T sym(void *lib,const char *name) {
    void *p=dlsym(lib,name);
    if(!p){emit({{"event","error"},{"message","Required plugin symbol missing"}});_exit(3);}
    return reinterpret_cast<T>(p);
}
static std::string str(const json &d,const char *key) {
    auto it=d.find(key);return it!=d.end()&&it->is_string()?it->get<std::string>():std::string();
}
static json parse(const std::string &s) {
    auto j=json::parse(s,nullptr,false);return j.is_object()?j:json::object();
}
static json pick(const json &d,std::initializer_list<const char*> keys) {
    auto safe=json::object();
    for(auto k:keys)if(d.contains(k))safe[k]=d[k];
    return safe;
}
static long long number(const json &v) {
    if(v.is_number_integer())return v.get<long long>();
    if(v.is_string()){try{return std::stoll(v.get<std::string>());}catch(...) {}}
    return 0;
}
int main(){
    // Suppress all vendor logging before loading the plugin. Only explicit,
    // allowlisted JSON events use the saved output descriptor.
    result_fd=dup(STDOUT_FILENO);
    int nullfd=open("/dev/null",O_WRONLY);dup2(nullfd,1);dup2(nullfd,2);close(nullfd);
    const std::string input{std::istreambuf_iterator<char>(std::cin),std::istreambuf_iterator<char>()};
    json cfg=json::parse(input,nullptr,false);
    if(!cfg.is_object())_exit(3);
    std::string action=str(cfg,"action"), serial=str(cfg,"serial"), ip=str(cfg,"ip");
    if(action!="status"&&action!="sign_probe"&&action!="start"&&action!="control"&&action!="bed_temp")_exit(3);
    const std::string expected_model=str(cfg,"expected_model");
    if((action=="start"||!expected_model.empty())&&expected_model!="C12"&&expected_model!="O1D")_exit(3);
    const std::string control=str(cfg,"control");
    if(action=="control"&&control!="pause"&&control!="resume"&&control!="stop")_exit(3);
    const long long target_bed=cfg["target_c"].is_number_integer()?number(cfg["target_c"]):-1;
    if(action=="bed_temp"&&(target_bed<0||target_bed>120))_exit(3);
    void *lib=dlopen(str(cfg,"plugin").c_str(),RTLD_NOW|RTLD_LOCAL);
    if(!lib){emit({{"event","error"},{"message","Plugin could not be loaded"}});_exit(3);}
    auto version=sym<std::string(*)()>(lib,"bambu_network_get_version")();
    emit({{"event","plugin"},{"version",version}});
    if(version.substr(0,8)!="02.08.02"){
        emit({{"event","error"},{"message","Plugin ABI changed; update the pinned adapter before printing"}});_exit(3);
    }
    void *agent=sym<void*(*)(std::string)>(lib,"bambu_network_create_agent")(str(cfg,"scratch"));
    if(!agent)_exit(3);
    sym<int(*)(void*,std::string)>(lib,"bambu_network_set_config_dir")(agent,str(cfg,"config_dir"));
    sym<int(*)(void*,std::string,std::string)>(lib,"bambu_network_set_cert_file")(agent,str(cfg,"cert_dir"),"slicer_base64.cer");
    sym<int(*)(void*,std::string)>(lib,"bambu_network_set_country_code")(agent,str(cfg,"country"));
    std::atomic<bool> connected{false},got_status{false},got_start{false},got_probe{false},rejected{false};
    std::atomic<bool> submitted{false},cert_ready{false};
    std::atomic<bool> control_ack{false},control_state{false},bed_ack{false},bed_state{false};
    std::mutex state_mutex;
    std::string current_state,current_job;
    long long current_error=0;
    bool error_known=false;
    const std::string expected=str(cfg,"job_name"),sequence=str(cfg,"sequence_id");
    BBL::OnLocalConnectedFn conn=[&](int status,std::string,std::string){if(status==0)connected=true;};
    BBL::OnPrinterConnectedFn pc=[&](std::string){connected=true;};
    BBL::OnMessageFn onmsg=[&](std::string,std::string message){
        if(message=="device_cert_installed"){cert_ready=true;emit({{"event","device_certificate_ready"}});return;}
        json root=parse(message),p=root["print"];
        json security=root["security"];
        if(security.is_object()) {
            json safe=pick(security,{"command","result","err_code","reason"});safe["event"]="certificate_reply";emit(safe);
            if(str(security,"command")=="app_cert_install"&&str(security,"result")=="SUCCESS")cert_ready=true;
        }
        if(!p.is_object())return;
        json safe=pick(p,{"command","sequence_id","gcode_state","subtask_name","stg_cur","mc_percent","layer_num","total_layer_num","mc_remaining_time","print_error","result","reason","err_code","bed_temper","bed_target_temper","nozzle_temper","nozzle_target_temper"});
        for(auto key:{"nozzle_diameter","nozzle_type"})if(p.contains(key))safe[key]=p[key];
        safe["event"]="telemetry";
        if(!p["ams"].is_null()){
            json trays=json::array();
            for(auto &a : p["ams"]["ams"]){
                for(auto &t : a["tray"]){
                    json tray=pick(t,{"id","tray_type","tray_color","tray_info_idx"});
                    tray["ams_id"]=a.value("id",json(""));trays.push_back(tray);
                }
            }
            safe["trays"]=trays;
        }
        if(!p["device"]["nozzle"].is_null()){
            json nozzles=json::array();
            for(auto &n : p["device"]["nozzle"]["info"])
                nozzles.push_back(pick(n,{"id","diameter","type"}));
            safe["nozzles"]=nozzles;
        }
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            if(!p["gcode_state"].is_null()){current_state=str(p,"gcode_state");got_status=true;}
            if(!p["subtask_name"].is_null())current_job=str(p,"subtask_name");
            if(!p["print_error"].is_null()){current_error=number(p["print_error"]);error_known=true;}
            if(submitted && current_job==expected && (current_state=="PREPARE"||current_state=="RUNNING"))got_start=true;
            if(action=="control"&&submitted&&!p["gcode_state"].is_null()){
                bool same_job=current_job==expected;
                bool stopped=control=="stop"&&(current_state=="IDLE"||current_state=="FAILED")&&(same_job||current_job.empty());
                control_state=stopped||(same_job&&((control=="pause"&&current_state=="PAUSE")||(control=="resume"&&current_state=="RUNNING")));
            }
            if(action=="bed_temp"&&submitted&&!p["bed_target_temper"].is_null()){
                if(number(p["bed_target_temper"])==target_bed)bed_state=true;
            }
        }
        if(str(p,"command")=="extrusion_cali_get" && str(p,"sequence_id")==sequence){
            got_probe=true;
            if(number(p["err_code"])!=0)rejected=true;
        }
        if(str(p,"command")=="project_file" && str(p,"sequence_id")==sequence){
            if(number(p["err_code"])!=0 || str(p,"result")=="FAIL" || str(p,"result")=="fail")rejected=true;
        }
        if(action=="control"&&submitted&&str(p,"command")==control&&str(p,"sequence_id")==sequence){
            const auto result=str(p,"result");
            if(number(p["err_code"])!=0||result=="FAIL"||result=="fail")rejected=true;
            else if(result=="SUCCESS"||result=="success")control_ack=true;
        }
        if(action=="bed_temp"&&submitted&&str(p,"command")=="gcode_line"&&str(p,"sequence_id")==sequence){
            const auto result=str(p,"result");
            if(number(p["err_code"])!=0||result=="FAIL"||result=="fail")rejected=true;
            else if(result=="SUCCESS"||result=="success")bed_ack=true;
        }
        emit(safe);
    };
    sym<int(*)(void*,BBL::OnLocalConnectedFn)>(lib,"bambu_network_set_on_local_connect_fn")(agent,conn);
    sym<int(*)(void*,BBL::OnPrinterConnectedFn)>(lib,"bambu_network_set_on_printer_connected_fn")(agent,pc);
    sym<int(*)(void*,BBL::OnMessageFn)>(lib,"bambu_network_set_on_local_message_fn")(agent,onmsg);
    sym<int(*)(void*,BBL::OnMessageFn)>(lib,"bambu_network_set_on_message_fn")(agent,onmsg);
    int rc=sym<int(*)(void*)>(lib,"bambu_network_start")(agent);
    if(rc!=0){emit({{"event","error"},{"message","Plugin startup failed"},{"rc",rc}});_exit(4);}
    if(!expected_model.empty()){
        BBL::detectResult detected{};
        rc=sym<int(*)(void*,std::string,std::string,BBL::detectResult&)>(lib,"bambu_network_bind_detect")(agent,ip,"secure",detected);
        if(rc!=0||detected.dev_id!=serial||detected.model_id!=expected_model){
            emit({{"event","error"},{"message","Printer identity or model does not match the sliced file"}});_exit(5);
        }
        emit({{"event","printer_identity"},{"model_id",detected.model_id}});
    }
    rc=sym<int(*)(void*,std::string,std::string,std::string,std::string,bool)>(lib,"bambu_network_connect_printer")(agent,serial,ip,"bblp",str(cfg,"access_code"),true);
    if(rc!=0)_exit(4);
    for(int i=0;i<150&&!connected;i++)std::this_thread::sleep_for(std::chrono::milliseconds(100));
    if(!connected){emit({{"event","error"},{"message","Printer connection timed out"}});_exit(4);}
    auto send=sym<int(*)(void*,std::string,std::string,int,int)>(lib,"bambu_network_send_message_to_printer");
    rc=send(agent,serial,R"({"pushing":{"sequence_id":"24999","command":"pushall","version":1,"push_target":1}})",1,0);
    emit({{"event","status_request"},{"rc",rc}});
    // Keep control requests within the launcher's overall 95-second deadline.
    const int status_wait_ticks=action=="status"?300:120;
    for(int i=0;i<status_wait_ticks&&!got_status;i++)std::this_thread::sleep_for(std::chrono::milliseconds(100));
    if(!got_status){emit({{"event","error"},{"message","No fresh printer status"}});_exit(4);}
    if(action=="status"){
        emit({{"event","status_complete"}});
        sym<int(*)(void*)>(lib,"bambu_network_disconnect_printer")(agent);_exit(0);
    }
    // Required lifecycle: initialization alone does not populate the device's
    // signing/encryption state. Use the same exported certificate APIs as Studio.
    rc=sym<int(*)(void*)>(lib,"bambu_network_update_cert")(agent);
    emit({{"event","certificate_refresh"},{"rc",rc}});
    // The boolean selects the existing LAN transport for this certificate
    // exchange. It does not toggle the printer's LAN Only setting.
    sym<void(*)(void*,std::string,bool)>(lib,"bambu_network_install_device_cert")(agent,serial,true);
    for(int i=0;i<100&&!cert_ready;i++)std::this_thread::sleep_for(std::chrono::milliseconds(100));
    if(!cert_ready){emit({{"event","error"},{"message","Device certificate exchange not confirmed"}});_exit(4);}
    std::string payload;
    if(action=="sign_probe"){
        payload="{\"print\":{\"command\":\"extrusion_cali_get\",\"sequence_id\":\""+sequence+"\",\"filament_id\":\"GFL99\",\"nozzle_diameter\":\"0.4\"}}";
    }else if(action=="bed_temp"){
        payload=str(cfg,"payload");
        json p=parse(payload)["print"];
        const std::string expected_param="M140 S"+std::to_string(target_bed)+"\n";
        if(str(p,"command")!="gcode_line"||str(p,"sequence_id")!=sequence||str(p,"param")!=expected_param)_exit(3);
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            bool idle=(current_state=="IDLE"||current_state=="FINISH"||current_state=="FAILED");
            bool matched_active=!expected.empty()&&current_job==expected&&(current_state=="PREPARE"||current_state=="RUNNING"||current_state=="PAUSE");
            if(!idle&&!matched_active){
                emit({{"event","error"},{"message","Job or state changed; bed temperature refused"}});_exit(5);
            }
            submitted=true;
        }
        emit({{"event","dispatching"}});
    }else if(action=="control"){
        payload=str(cfg,"payload");
        json p=parse(payload)["print"];
        if(expected.empty()||str(p,"command")!=control||str(p,"sequence_id")!=sequence||str(p,"param")!="")_exit(3);
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            bool allowed=(control=="pause"&&current_state=="RUNNING")||
                (control=="resume"&&current_state=="PAUSE"&&error_known&&current_error==0)||
                (control=="stop"&&(current_state=="PREPARE"||current_state=="RUNNING"||current_state=="PAUSE"));
            if(current_job!=expected||!allowed){
                emit({{"event","error"},{"message","Job or state changed; control refused"}});_exit(5);
            }
            submitted=true;
        }
        emit({{"event","dispatching"}});
    }else{
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            if(current_state!="IDLE"&&current_state!="FINISH"&&current_state!="FAILED"){
                emit({{"event","error"},{"message","Printer is busy; start refused"}});_exit(5);
            }
        }
        payload=str(cfg,"payload");
        json p=parse(payload)["print"];
        if(str(p,"command")!="project_file"||str(p,"sequence_id")!=sequence||str(p,"subtask_name")!=expected)_exit(3);
        submitted=true;
        emit({{"event","dispatching"}});
    }
    rc=send(agent,serial,payload,1,0);
    emit({{"event","send_result"},{"rc",rc}});
    if(rc!=0)_exit(6);
    auto complete=[&](){
        if(action=="control")return control_ack.load()&&control_state.load();
        if(action=="bed_temp")return bed_state.load()&&!rejected.load();
        return action=="start"?got_start.load():got_probe.load();
    };
    for(int i=0;i<450&&!rejected&&!complete();i++)
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    bool confirmed=complete();
    emit({{"event","verification"},{"confirmed",confirmed&&!rejected},{"rejected",rejected.load()}});
    sym<int(*)(void*)>(lib,"bambu_network_disconnect_printer")(agent);
    // Vendor worker teardown may block. Disconnect first; process exit closes
    // remaining sockets and destroys all in-memory credentials deterministically.
    _exit(confirmed&&!rejected?0:7);
}
